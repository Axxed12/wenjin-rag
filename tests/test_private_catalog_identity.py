"""私域共享目录只在显式route选择且可用时登记，并服从reader授权。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from http.cookies import SimpleCookie
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from starlette.testclient import TestClient

from wenjin.content_hashing import TEXT_NEWLINE_LF_V1, hash_content
from wenjin.file_management import FileManagementService
from wenjin.indexing.build import build_candidate_index
from wenjin.management_http.app import create_app
from wenjin.releases.catalog import ReleaseCatalog
from wenjin.releases.resolver import release_catalog_path
from wenjin.retrieval.errors import StaleSnapshotError
from wenjin.retrieval.legacy_catalog import LegacySearchService

pytestmark = pytest.mark.integration

_ADMIN_TOKEN = "synthetic-private-catalog-bootstrap-token"
_ADMIN_PASSWORD = "Private_Catalog_Admin_Only_2026!"
_READER_TEMP_PASSWORD = "Private_Catalog_Reader_Temp_2026!"
_READER_PASSWORD = "Private_Catalog_Reader_Ready_2026!"
_EMBEDDING_MODEL = "synthetic-private-catalog-embedder-v1"


class _MarkerEmbedder:
    dimension = 16

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [
            int(hashlib.sha256(f"{index}:{text}".encode()).hexdigest()[:4], 16) / 65535.0
            for index in range(16)
        ]

    def get_text_embedding_batch(self, texts, show_progress=False):
        del show_progress
        return [self._vector(text) for text in texts]

    def get_query_embedding(self, query):
        return self._vector(query)


def _build_private_target(root: Path, qdrant, library_id: str, version: str):
    corpus_id = f"{library_id}-{version}"
    relative = "private-guide.txt"
    marker = f"private_marker_{version.replace('.', '_')}"
    content = f"Synthetic private source {library_id} {version}.\nUnique marker: {marker}.\n".encode()
    hashed = hash_content(content, normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8")
    raw_subdir = f"{library_id}/{version}"
    raw_path = root / "raw_data" / raw_subdir / relative
    manifest_path = root / "corpora-private" / corpus_id / "source-manifest.json"
    raw_path.parent.mkdir(parents=True)
    manifest_path.parent.mkdir(parents=True)
    raw_path.write_bytes(content)
    manifest = {
        "schema_version": 3,
        "corpus_id": corpus_id,
        "library": library_id,
        "version": version,
        "license": "synthetic-test-only",
        "source_url": f"urn:synthetic:{corpus_id}",
        "raw_data_subdir": raw_subdir,
        "files": [{
            "path": relative,
            "raw_sha256": hashed.raw_sha256,
            "canonical_sha256": hashed.canonical_sha256,
            "normalization": TEXT_NEWLINE_LF_V1,
            "encoding": "utf-8",
            "bytes": len(content),
            "indexable": True,
            "source_url": f"urn:synthetic:{corpus_id}/{relative}",
        }],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    target = build_candidate_index(
        manifest_path=manifest_path,
        target_id=f"{library_id}-target-{version.replace('.', '-')}",
        strategy="blocks-split",
        dense_collection=f"private_{library_id.replace('-', '_')}_{version.replace('.', '_')}",
        bm25_path=root / "data" / "indexes-private" / f"{library_id}-{version}.sqlite",
        qdrant_client=qdrant,
        embedder=_MarkerEmbedder(),
        embedding_model=_EMBEDDING_MODEL,
        project_root=root,
    )
    config_dir = root / "configs" / "indexes-private"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / f"{target.target_id}.json").write_text(
        json.dumps(target.__dict__), encoding="utf-8"
    )
    return target, corpus_id, marker, relative


def _write_private_route(root: Path, name: str, *targets) -> Path:
    route = root / "configs" / "indexes-private" / f"{name}-route.json"
    route.parent.mkdir(parents=True, exist_ok=True)
    route.write_text(
        json.dumps({
            "schema_version": 1,
            "candidates": [target.__dict__ for target in targets],
        }),
        encoding="utf-8",
    )
    return route


def _catalog_source(service, library_id: str, corpus_id: str):
    group = next(
        item for item in service.catalog()["libraries"] if item["library_id"] == library_id
    )
    return next(
        item for item in group["sources"]
        if item.get("corpus_id") == corpus_id and item["source_type"] == "shared"
    )


def _headers(csrf: str) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-CSRF-Token": csrf}


def _login(client, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login",
        headers={"Origin": "http://testserver"},
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


class _SessionClient:
    """同一个TestClient中显式切换身份，避免cookie串用。"""

    def __init__(self, client):
        self.client = client
        self.token = None

    def request(self, method, url, **kwargs):
        previous = self.client.cookies.get("wenjin_session")
        self.client.cookies.delete("wenjin_session")
        if self.token:
            self.client.cookies.set("wenjin_session", self.token)
        try:
            response = self.client.request(method, url, **kwargs)
            cookie = SimpleCookie()
            cookie.load(response.headers.get("set-cookie", ""))
            if "wenjin_session" in cookie:
                self.token = cookie["wenjin_session"].value
            return response
        finally:
            self.client.cookies.delete("wenjin_session")
            if previous:
                self.client.cookies.set("wenjin_session", previous)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


def test_private_route_selects_one_target_and_rejects_candidates_ambiguous_or_broken(
    tmp_path,
):
    qdrant = QdrantClient(":memory:")
    try:
        root = tmp_path / "private-catalog-root"
        root.mkdir()
        service = LegacySearchService(root, qdrant, _MarkerEmbedder())
        target_a, corpus_a, _marker, _path = _build_private_target(
            root, qdrant, "private-catalog", "1.0"
        )

        # 裸target JSON是候选；没有显式route时不能成为effective/可授权资源。
        candidate = _catalog_source(service, "private-catalog", corpus_a)
        assert candidate["is_active"] is False
        assert candidate["searchable"] is False
        assert candidate["version_id"] is None

        invalid_path = root / "configs" / "indexes-private" / "private-catalog-invalid-route.json"
        invalid_path.write_text(
            json.dumps({"schema_version": 99, "candidates": [target_a.__dict__]}),
            encoding="utf-8",
        )
        invalid_route_candidate = _catalog_source(service, "private-catalog", corpus_a)
        assert invalid_route_candidate["is_active"] is False
        assert invalid_route_candidate["searchable"] is False
        invalid_path.unlink()

        route = _write_private_route(root, "private-catalog", target_a)
        selected = _catalog_source(service, "private-catalog", corpus_a)
        assert selected["source_scope"] == "private_config"
        assert selected["is_active"] is True
        assert selected["searchable"] is True
        assert selected["version_id"]

        # 相同target被多条route重复声明时按target身份去重，不形成歧义。
        _write_private_route(root, "private-catalog-copy", target_a)
        assert _catalog_source(service, "private-catalog", corpus_a)["is_active"] is True

        # 同一library出现不同route target时不自行选一个，也不给任何一版活动身份。
        target_b, corpus_b, _marker_b, _path_b = _build_private_target(
            root, qdrant, "private-catalog", "2.0"
        )
        _write_private_route(root, "private-catalog-second", target_b)
        assert _catalog_source(service, "private-catalog", corpus_a)["is_active"] is False
        assert _catalog_source(service, "private-catalog", corpus_b)["is_active"] is False

        # 只保留一个选择，但删掉索引collection后，快速校验失败不能标活动。
        route.unlink()
        (root / "configs" / "indexes-private" / "private-catalog-copy-route.json").unlink()
        _write_private_route(root, "private-catalog-only", target_a)
        qdrant.delete_collection(target_a.dense_collection)
        broken = _catalog_source(service, "private-catalog", corpus_a)
        assert broken["is_active"] is False
        assert broken["searchable"] is False
        assert broken["version_id"] is None
    finally:
        qdrant.close()


def test_private_route_does_not_fallback_after_release_pointer_was_activated(tmp_path):
    qdrant = QdrantClient(":memory:")
    try:
        root = tmp_path / "private-release-root"
        root.mkdir()
        target, corpus_id, _marker, _path = _build_private_target(
            root, qdrant, "private-release", "1.0"
        )
        _write_private_route(root, "private-release", target)
        release_catalog = ReleaseCatalog(release_catalog_path(root))
        release_catalog.register({
            "release_id": "private-release-active-record",
            "library_id": "private-release",
            "status": "preparing",
            "created_at": "2026-10-12T00:00:00+00:00",
        })
        release_catalog.mark_ready(
            "private-release-active-record",
            seal={
                "target": target.__dict__,
                "release_digest": "a" * 64,
                "source_hashes": {},
                "validation": {"status": "valid"},
            },
        )
        release_catalog.activate(
            "private-release", "private-release-active-record",
            expected_revision=0, reason="synthetic activation before pointer loss",
        )
        # 模拟曾成功激活后active指针损坏；event仍证明该库已迁移，不能回退到route。
        with sqlite3.connect(release_catalog_path(root)) as database:
            database.execute(
                "UPDATE libraries SET active_release_id=NULL WHERE library_id=?",
                ("private-release",),
            )
        service = LegacySearchService(root, qdrant, _MarkerEmbedder())
        assert all(
            item["library_id"] != "private-release"
            for item in service.catalog()["libraries"]
        )
        with pytest.raises(StaleSnapshotError):
            service.validate_selection("private-release", corpus_id, target.target_id)
    finally:
        qdrant.close()


def test_private_route_catalog_grant_search_source_and_immediate_revoke(tmp_path):
    qdrant = QdrantClient(":memory:")
    root = tmp_path / "private-identity-root"
    root.mkdir()
    target, corpus_id, marker, relative = _build_private_target(
        root, qdrant, "private-identity", "1.0"
    )
    _write_private_route(root, "private-identity", target)
    manager = FileManagementService(
        root=root,
        qdrant_client=qdrant,
        embedder=_MarkerEmbedder(),
        embedding_model=_EMBEDDING_MODEL,
    )
    app = create_app(
        root,
        admin_token=_ADMIN_TOKEN,
        manager=manager,
        identity_db=root / "data" / "identity" / "identity.sqlite",
    )
    try:
        with TestClient(app, client=("127.0.0.1", 53141)) as client:
            bootstrap = client.post(
                "/api/auth/bootstrap",
                headers={"Authorization": f"Bearer {_ADMIN_TOKEN}", "Origin": "http://testserver"},
                json={"username": "private-admin", "password": _ADMIN_PASSWORD},
            )
            assert bootstrap.status_code == 201, bootstrap.text
            admin_client = _SessionClient(client)
            reader_client = _SessionClient(client)
            mcp_client = _SessionClient(client)
            admin = _login(admin_client, "private-admin", _ADMIN_PASSWORD)
            reader_create = admin_client.post(
                "/api/admin/users",
                headers=_headers(admin["csrf_token"]),
                json={"username": "private-reader", "password": _READER_TEMP_PASSWORD, "role": "reader"},
            )
            assert reader_create.status_code == 201, reader_create.text
            reader_user = reader_create.json()["user"]
            first_login = _login(reader_client, "private-reader", _READER_TEMP_PASSWORD)
            changed = reader_client.post(
                "/api/auth/password",
                headers=_headers(first_login["csrf_token"]),
                json={"old_password": _READER_TEMP_PASSWORD, "new_password": _READER_PASSWORD},
            )
            assert changed.status_code == 200, changed.text
            reader = _login(reader_client, "private-reader", _READER_PASSWORD)
            reader_headers = _headers(reader["csrf_token"])
            credential = reader_client.post(
                "/api/auth/credentials",
                headers=reader_headers,
                json={"label": "private-catalog-reader"},
            )
            assert credential.status_code == 201, credential.text
            mcp_headers = {"Authorization": f"Bearer {credential.json()['credential']['token']}"}
            library_id = "private-identity"

            # 资源目录发现只登记可用资源，不自动为reader创建grant。
            admin_resources = admin_client.get("/api/admin/resources")
            assert admin_resources.status_code == 200, admin_resources.text
            resource = next(
                row for row in admin_resources.json()["resources"]
                if row["resource_kind"] == "library" and row["resource_id"] == library_id
            )
            assert bool(resource["active"])
            assert all(
                not (grant["resource_kind"] == "library" and grant["resource_id"] == library_id
                     and grant["user_id"] == reader_user["user_id"] and grant["allowed"])
                for grant in admin_resources.json()["grants"]
            )

            admin_catalog = admin_client.get("/api/admin/libraries")
            assert admin_catalog.status_code == 200, admin_catalog.text
            discovered_library = next(
                item for item in admin_catalog.json()["libraries"]
                if item["library_id"] == library_id
            )
            discovered_source = next(
                item for item in discovered_library["sources"]
                if item.get("corpus_id") == corpus_id
            )
            assert discovered_library["classification"] == {
                "classification": "private",
                "revision": 0,
                "defaulted": True,
                "source_visibility": "private",
            }
            assert discovered_library["source_visibility"] == "private"
            assert discovered_library["readable"] is True
            assert discovered_source["publication_status"] == "published"
            assert discovered_source["readable"] is True
            assert discovered_source["searchable"] is True

            libraries = reader_client.get("/api/admin/libraries")
            assert libraries.status_code == 200
            assert all(item["library_id"] != library_id for item in libraries.json()["libraries"])
            assert all(
                item["library_id"] != library_id
                for item in mcp_client.get("/api/mcp/libraries", headers=mcp_headers).json()["libraries"]
            )
            search_payload = {
                "source_type": "shared",
                "library_id": library_id,
                "corpus_id": corpus_id,
                "index_target_id": target.target_id,
                "version_id": _catalog_source(
                    LegacySearchService(
                        root, qdrant, _MarkerEmbedder(),
                        expected_embedding_model=_EMBEDDING_MODEL,
                    ), library_id, corpus_id
                )["version_id"],
                "query": marker,
            }
            assert client.get(
                f"/api/admin/libraries/{library_id}/classification"
            ).status_code == 401
            admin_default = admin_client.get(
                f"/api/admin/libraries/{library_id}/classification"
            )
            assert admin_default.status_code == 200
            assert admin_default.json()["classification"]["defaulted"] is True
            malformed_classification = admin_client.request(
                "PUT",
                f"/api/admin/libraries/{library_id}/classification",
                headers=_headers(admin["csrf_token"]),
                json={"classification": [], "expected_revision": 0},
            )
            assert malformed_classification.status_code == 422
            reader_cannot_classify = reader_client.request(
                "PUT",
                f"/api/admin/libraries/{library_id}/classification",
                headers=_headers(reader["csrf_token"]),
                json={"classification": "public", "expected_revision": 0},
            )
            assert reader_cannot_classify.status_code == 403
            classified_public = admin_client.request(
                "PUT",
                f"/api/admin/libraries/{library_id}/classification",
                headers=_headers(admin["csrf_token"]),
                json={"classification": "public", "expected_revision": 0},
            )
            assert classified_public.status_code == 200, classified_public.text
            assert classified_public.json()["classification"] == {
                "library_id": library_id,
                "classification": "public",
                "revision": 1,
                "defaulted": False,
                "source_visibility": "private",
            }
            stale_classification = admin_client.request(
                "PUT",
                f"/api/admin/libraries/{library_id}/classification",
                headers=_headers(admin["csrf_token"]),
                json={"classification": "private", "expected_revision": 0},
            )
            assert stale_classification.status_code == 409
            public_labeled = next(
                item for item in admin_client.get("/api/admin/libraries").json()["libraries"]
                if item["library_id"] == library_id
            )
            assert public_labeled["classification"]["classification"] == "public"
            assert public_labeled["source_visibility"] == "private"
            grants_after_label = admin_client.get("/api/admin/resources").json()["grants"]
            assert all(
                not (grant["resource_kind"] == "library" and grant["resource_id"] == library_id
                     and grant["user_id"] == reader_user["user_id"] and grant["allowed"])
                for grant in grants_after_label
            )

            admin_search = admin_client.post(
                "/api/admin/search",
                headers=_headers(admin["csrf_token"]),
                json=search_payload,
            )
            assert admin_search.status_code == 200, admin_search.text
            admin_source = admin_client.get(
                f"/api/libraries/{library_id}/source",
                params={
                    "corpus_id": corpus_id,
                    "index_target_id": target.target_id,
                    "version_id": search_payload["version_id"],
                    "document_id": LegacySearchService._document_id(corpus_id, relative),
                },
            )
            assert admin_source.status_code == 200, admin_source.text
            admin_credential = admin_client.post(
                "/api/auth/credentials",
                headers=_headers(admin["csrf_token"]),
                json={"label": "private-catalog-admin"},
            )
            assert admin_credential.status_code == 201, admin_credential.text
            admin_mcp_headers = {
                "Authorization": f"Bearer {admin_credential.json()['credential']['token']}"
            }
            admin_mcp_libraries = client.get(
                "/api/mcp/libraries", headers=admin_mcp_headers
            )
            assert admin_mcp_libraries.status_code == 200, admin_mcp_libraries.text
            assert any(
                item["library_id"] == library_id
                for item in admin_mcp_libraries.json()["libraries"]
            )
            admin_mcp_search = client.post(
                "/api/mcp/search", headers=admin_mcp_headers, json=search_payload
            )
            assert admin_mcp_search.status_code == 200, admin_mcp_search.text
            admin_mcp_source = client.get(
                f"/api/mcp/libraries/{library_id}/source",
                headers=admin_mcp_headers,
                params={
                    "corpus_id": corpus_id,
                    "index_target_id": target.target_id,
                    "version_id": search_payload["version_id"],
                    "document_id": LegacySearchService._document_id(corpus_id, relative),
                },
            )
            assert admin_mcp_source.status_code == 200, admin_mcp_source.text
            denied_search = reader_client.post(
                "/api/admin/search", headers=reader_headers, json=search_payload
            )
            assert denied_search.status_code == 403, denied_search.text
            denied_source = reader_client.get(
                f"/api/libraries/{library_id}/source",
                params={
                    "corpus_id": corpus_id,
                    "index_target_id": target.target_id,
                    "version_id": search_payload["version_id"],
                    "document_id": LegacySearchService._document_id(corpus_id, relative),
                },
            )
            assert denied_source.status_code == 403, denied_source.text
            assert mcp_client.post(
                "/api/mcp/search", headers=mcp_headers, json=search_payload
            ).status_code == 403
            mcp_denied_source = mcp_client.get(
                f"/api/mcp/libraries/{library_id}/source",
                headers=mcp_headers,
                params={
                    "corpus_id": corpus_id,
                    "index_target_id": target.target_id,
                    "version_id": search_payload["version_id"],
                    "document_id": LegacySearchService._document_id(corpus_id, relative),
                },
            )
            assert mcp_denied_source.status_code == 403

            grant = admin_client.post(
                "/api/admin/grants",
                headers=_headers(admin["csrf_token"]),
                json={
                    "user_id": reader_user["user_id"],
                    "resource_kind": "library",
                    "resource_id": library_id,
                    "allowed": True,
                    "expected_revision": 0,
                },
            )
            assert grant.status_code == 200, grant.text
            classified_private = admin_client.request(
                "PUT",
                f"/api/admin/libraries/{library_id}/classification",
                headers=_headers(admin["csrf_token"]),
                json={"classification": "private", "expected_revision": 1},
            )
            assert classified_private.status_code == 200, classified_private.text
            libraries = reader_client.get("/api/admin/libraries")
            visible = next(item for item in libraries.json()["libraries"] if item["library_id"] == library_id)
            assert visible["classification"]["classification"] == "private"
            assert visible["classification"]["revision"] == 2
            visible_source = next(item for item in visible["sources"] if item.get("corpus_id") == corpus_id)
            assert visible_source["is_active"] is True
            assert visible_source["searchable"] is True
            search_payload["version_id"] = visible_source["version_id"]
            searched = reader_client.post(
                "/api/admin/search", headers=reader_headers, json=search_payload
            )
            assert searched.status_code == 200, searched.text
            assert any(marker in item["text"] for item in searched.json()["evidence"])
            source = reader_client.get(
                f"/api/libraries/{library_id}/source",
                params={
                    "corpus_id": corpus_id,
                    "index_target_id": target.target_id,
                    "version_id": visible_source["version_id"],
                    "document_id": visible_source["documents"][0]["document_id"],
                },
            )
            assert source.status_code == 200, source.text
            assert marker.encode() in source.content
            mcp_libraries = mcp_client.get("/api/mcp/libraries", headers=mcp_headers)
            assert mcp_libraries.status_code == 200, mcp_libraries.text
            assert any(item["library_id"] == library_id for item in mcp_libraries.json()["libraries"])
            mcp_search = mcp_client.post(
                "/api/mcp/search", headers=mcp_headers, json=search_payload
            )
            assert mcp_search.status_code == 200, mcp_search.text
            mcp_source = mcp_client.get(
                f"/api/mcp/libraries/{library_id}/source",
                headers=mcp_headers,
                params={
                    "corpus_id": corpus_id,
                    "index_target_id": target.target_id,
                    "version_id": visible_source["version_id"],
                    "document_id": visible_source["documents"][0]["document_id"],
                },
            )
            assert mcp_source.status_code == 200, mcp_source.text
            assert marker.encode() in mcp_source.content

            revoked = admin_client.post(
                "/api/admin/grants",
                headers=_headers(admin["csrf_token"]),
                json={
                    "user_id": reader_user["user_id"],
                    "resource_kind": "library",
                    "resource_id": library_id,
                    "allowed": False,
                    "expected_revision": grant.json()["grant"]["revision"],
                },
            )
            assert revoked.status_code == 200, revoked.text
            assert all(
                item["library_id"] != library_id
                for item in reader_client.get("/api/admin/libraries").json()["libraries"]
            )
            assert reader_client.post(
                "/api/admin/search", headers=reader_headers, json=search_payload
            ).status_code == 403
            assert reader_client.get(
                f"/api/libraries/{library_id}/source",
                params={
                    "corpus_id": corpus_id,
                    "index_target_id": target.target_id,
                    "version_id": visible_source["version_id"],
                    "document_id": visible_source["documents"][0]["document_id"],
                },
            ).status_code == 403
            assert mcp_client.get(
                "/api/mcp/libraries", headers=mcp_headers
            ).json()["libraries"] == [] or all(
                item["library_id"] != library_id
                for item in mcp_client.get("/api/mcp/libraries", headers=mcp_headers).json()["libraries"]
            )
            assert mcp_client.post(
                "/api/mcp/search", headers=mcp_headers, json=search_payload
            ).status_code == 403
            assert mcp_client.get(
                f"/api/mcp/libraries/{library_id}/source",
                headers=mcp_headers,
                params={
                    "corpus_id": corpus_id,
                    "index_target_id": target.target_id,
                    "version_id": visible_source["version_id"],
                    "document_id": visible_source["documents"][0]["document_id"],
                },
            ).status_code == 403
    finally:
        qdrant.close()
