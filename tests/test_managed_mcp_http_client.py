"""HTTP bridge response validation and secret-safe error mapping contracts."""

from __future__ import annotations

import json

import httpx
import pytest

from wenjin.managed_mcp import (
    ManagedFileConflict,
    ManagedFileHTTPClient,
    ManagedFileServiceError,
)

FILE_ID = "a" * 32
OTHER_FILE_ID = "b" * 32
VERSION_ID = "c" * 32
OTHER_VERSION_ID = "d" * 32
SOURCE_SHA = "1" * 64
OTHER_SHA = "2" * 64
ADMIN_TOKEN = "test-managed-admin-token-never-return-this"
BACKEND_DETAIL = "private-backend-path and response-secret=" + ADMIN_TOKEN


def valid_payload() -> dict:
    evidence = {
        "file_id": FILE_ID,
        "version_id": VERSION_ID,
        "version": "2",
        "source_sha256": SOURCE_SHA,
        "text": "verified test evidence",
        "source_url": "urn:internal:source",
    }
    return {
        "version": "2",
        "collection": "fm_internal_collection",
        "index_target_id": "private-index-target",
        "managed_file": {
            "file_id": FILE_ID,
            "version_id": VERSION_ID,
            "version": "2",
            "source_sha256": SOURCE_SHA,
        },
        "evidence": [evidence],
    }


def client_for(handler) -> tuple[ManagedFileHTTPClient, httpx.Client]:
    transport = httpx.MockTransport(handler)
    raw_client = httpx.Client(transport=transport, trust_env=False)
    return ManagedFileHTTPClient("http://managed.test", ADMIN_TOKEN, client=raw_client), raw_client


def test_search_forwards_active_snapshot_parameters_and_strips_internal_fields() -> None:
    observed = {}

    def respond(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("authorization")
        observed["body"] = json.loads(request.content)
        return httpx.Response(200, json=valid_payload())

    bridge, raw_client = client_for(respond)
    try:
        result = bridge.search_file(
            FILE_ID,
            "marker query",
            version="2",
            top_k=3,
            context_char_budget=2400,
        )
    finally:
        bridge.close()
        raw_client.close()

    assert observed == {
        "authorization": f"Bearer {ADMIN_TOKEN}",
        "body": {
            "file_id": FILE_ID,
            "query": "marker query",
            "top_k": 3,
            "context_char_budget": 2400,
            "version": "2",
        },
    }
    assert result["evidence"][0]["text"] == "verified test evidence"
    assert result["evidence"][0].get("source_url") is None
    assert "collection" not in result and "index_target_id" not in result


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("managed_file"),
        lambda payload: payload["managed_file"].update(file_id=OTHER_FILE_ID),
        lambda payload: payload["managed_file"].update(version_id="invalid"),
        lambda payload: payload["managed_file"].update(source_sha256="bad-hash"),
        lambda payload: payload.update(version="3"),
        lambda payload: payload["evidence"][0].update(file_id=OTHER_FILE_ID),
        lambda payload: payload["evidence"][0].update(version_id=OTHER_VERSION_ID),
        lambda payload: payload["evidence"][0].update(version="old"),
        lambda payload: payload["evidence"][0].update(source_sha256=OTHER_SHA),
    ],
    ids=[
        "missing-metadata", "wrong-file-id", "malformed-version-id", "malformed-raw-sha",
        "top-level-version-mismatch", "wrong-evidence-file", "wrong-evidence-version-id",
        "wrong-evidence-version", "wrong-evidence-sha",
    ],
)
def test_inconsistent_identity_metadata_rejects_all_evidence(mutation) -> None:
    payload = valid_payload()
    mutation(payload)

    bridge, raw_client = client_for(lambda request: httpx.Response(200, json=payload))
    try:
        with pytest.raises(ManagedFileServiceError) as caught:
            bridge.search_file(FILE_ID, "marker query")
    finally:
        bridge.close()
        raw_client.close()

    assert "verified test evidence" not in str(caught.value)
    assert ADMIN_TOKEN not in str(caught.value)
    assert BACKEND_DETAIL not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "exception"),
    [(404, KeyError), (409, ManagedFileConflict), (422, ManagedFileConflict), (503, ManagedFileServiceError)],
)
def test_http_errors_never_forward_backend_body_or_token(status, exception) -> None:
    bridge, raw_client = client_for(
        lambda request: httpx.Response(status, text=BACKEND_DETAIL)
    )
    try:
        with pytest.raises(exception) as caught:
            bridge.search_file(FILE_ID, "marker query")
    finally:
        bridge.close()
        raw_client.close()

    assert ADMIN_TOKEN not in str(caught.value)
    assert "private-backend-path" not in str(caught.value)
    assert "response-secret" not in str(caught.value)


@pytest.mark.parametrize("invalid_json", ["not-json", "[]", "{\"files\": null}"])
def test_invalid_list_json_maps_to_secret_safe_service_error(invalid_json: str) -> None:
    bridge, raw_client = client_for(lambda request: httpx.Response(200, text=invalid_json))
    try:
        with pytest.raises(ManagedFileServiceError) as caught:
            bridge.list_files()
    finally:
        bridge.close()
        raw_client.close()

    assert ADMIN_TOKEN not in str(caught.value)
    assert BACKEND_DETAIL not in str(caught.value)


def test_invalid_search_json_maps_to_secret_safe_service_error() -> None:
    bridge, raw_client = client_for(lambda request: httpx.Response(200, text=BACKEND_DETAIL))
    try:
        with pytest.raises(ManagedFileServiceError) as caught:
            bridge.search_file(FILE_ID, "marker query")
    finally:
        bridge.close()
        raw_client.close()

    assert ADMIN_TOKEN not in str(caught.value)
    assert "private-backend-path" not in str(caught.value)
