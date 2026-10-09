from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from starlette.testclient import TestClient

import wenjin.management_http.assets as admin_assets
from wenjin.file_management import FileManagementService
from wenjin.file_management.api import create_app
from wenjin.file_management.models import StaleSnapshotError


class MarkerEmbedder:
    """隔离测试用的确定性向量器，不代表生产模型质量。"""

    dimension = 32

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in text.lower().split():
            index = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % self.dimension
            vector[index] += 1.0
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / norm for value in vector]

    def get_text_embedding_batch(self, texts, show_progress=False):
        return [self._vector(text) for text in texts]

    def get_query_embedding(self, query):
        return self._vector(query)


def _service(root: Path, client: QdrantClient) -> FileManagementService:
    return FileManagementService(
        root=root,
        qdrant_client=client,
        embedder=MarkerEmbedder(),
        embedding_model="isolated-marker-test-only",
    )


def test_jobs_api_lists_real_replacement_and_delete_history_without_payload_paths(tmp_path):
    qdrant = QdrantClient(":memory:")
    manager = _service(tmp_path, qdrant)
    first_source = tmp_path / "guide-v1.txt"
    first_source.write_text("amberwidget first version", encoding="utf-8")
    first = manager.submit_import(first_source, display_name="guide.txt")
    assert manager.run_job(first.job_id).status == "completed"
    first_version = manager.get_job(first.job_id).version

    second_source = tmp_path / "guide-v2.txt"
    second_source.write_text("amberwidget second version", encoding="utf-8")
    replacement = manager.submit_import(
        second_source, display_name="guide.txt", replace_file_id=first.file_id
    )
    assert manager.run_job(replacement.job_id).status == "completed"
    second_version = manager.get_job(replacement.job_id).version
    assert first_version != second_version

    disposable_source = tmp_path / "disposable.txt"
    disposable_source.write_text("deletable zephyrmarker", encoding="utf-8")
    disposable = manager.submit_import(disposable_source)
    assert manager.run_job(disposable.job_id).status == "completed"
    deletion = manager.submit_delete(disposable.file_id)
    assert manager.run_job(deletion.job_id).status == "deleted"

    # 给持久登记写入一个仅供泄露检查的源路径；列表SQL应根本不读取这些列。
    with manager._connect() as db:
        db.execute(
            "UPDATE jobs SET payload=?,resources=? WHERE job_id=?",
            ('{"source":"C:\\\\private\\\\payload.txt"}',
             '{"manifest":"/private/index/manifest.json"}', first.job_id),
        )

    app = create_app(tmp_path, manager=manager, admin_token="test-secret", max_upload_bytes=12345)
    try:
        with TestClient(app) as client:
            denied = client.get("/api/jobs")
            assert denied.status_code == 401
            headers = {"Authorization": "Bearer test-secret"}
            response = client.get("/api/jobs?limit=2&offset=0", headers=headers)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            body = response.json()
            assert body["limit"] == 2 and body["offset"] == 0
            assert [item["job_id"] for item in body["jobs"]] == [deletion.job_id, disposable.job_id]
            replacement_page = client.get("/api/jobs?limit=1&offset=2", headers=headers).json()
            assert replacement_page["jobs"][0]["job_id"] == replacement.job_id
            assert replacement_page["jobs"][0]["version"] == second_version
            assert replacement_page["jobs"][0]["resources"] is None
            assert replacement_page["jobs"][0]["duplicate_of"] is None

            older = client.get("/api/jobs?limit=1&offset=3", headers=headers).json()
            assert older["jobs"][0]["job_id"] == first.job_id
            assert older["jobs"][0]["version"] == first_version
            serialized = response.text + replacement_page.__str__() + client.get("/api/jobs?offset=3", headers=headers).text
            assert "payload.txt" not in serialized
            assert "manifest.json" not in serialized
            assert "private" not in serialized

            config_denied = client.get("/api/admin/config")
            assert config_denied.status_code == 401
            config = client.get("/api/admin/config", headers=headers)
            assert config.status_code == 200
            config_body = config.json()
            assert set(config_body) == {
                "max_upload_bytes", "authentication_required", "authentication_mode", "retrieval",
            }
            assert config_body["max_upload_bytes"] == 12345
            assert config_body["authentication_required"] is True
            assert config_body["authentication_mode"] == "bearer"
            assert set(config_body["retrieval"]) == {
                "candidate_k", "base_candidate_k", "rerank_pool", "top_k",
                "context_char_budget", "reranker_enabled", "reranker_model_id",
                "reranker_max_length", "reranker_batch_size", "rrf_k", "max_libraries",
            }
            assert config_body["retrieval"]["candidate_k"] == 10
            assert config_body["retrieval"]["reranker_enabled"] is False
            assert not any(
                token in config.text.lower()
                for token in ("api_key", "secret", "password", "embedding_model_path", "reranker_model_path")
            )
            assert "test-secret" not in config.text
            assert "tmp_path" not in config.text
    finally:
        manager.close()
        qdrant.close()


@pytest.mark.parametrize(
    "query",
    [
        "?limit=0",
        "?limit=201",
        "?limit=-1",
        "?limit=1.5",
        "?limit=999999999999999999999999999999999999999999",
        "?offset=-1",
        "?offset=9223372036854775808",
        "?offset=999999999999999999999999999999999999999999",
        "?offset=1&offset=2",
    ],
)
def test_jobs_api_rejects_invalid_pagination_with_422(tmp_path, query):
    qdrant = QdrantClient(":memory:")
    manager = _service(tmp_path, qdrant)
    app = create_app(tmp_path, manager=manager, admin_token="secret")
    try:
        with TestClient(app) as client:
            response = client.get("/api/jobs" + query, headers={"Authorization": "Bearer secret"})
            assert response.status_code == 422, response.text
    finally:
        manager.close()
        qdrant.close()


def test_admin_static_allowlist_redirect_headers_and_no_auth(tmp_path, monkeypatch):
    test_assets = {
        "index.html": b'<script src="/admin/app.js"></script>',
        "app.js": b"document.documentElement.dataset.ready = 'yes';",
        "styles.css": b".progress { width: 1%; }",
    }
    monkeypatch.setattr(
        admin_assets,
        "_read_admin_asset",
        lambda name: test_assets[name],
    )

    class StaticManager:
        def resume_pending_jobs(self):
            return []

        def close(self):
            pass

    app = create_app(tmp_path, manager=StaticManager(), admin_token="secret")
    with TestClient(app) as client:
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 307 and root.headers["location"] == "/admin/"
        page = client.get("/admin/")
        assert page.status_code == 200
        assert "Bearer" not in page.text
        script = client.get("/admin/app.js")
        assert script.status_code == 200
        assert "dataset.ready" in script.text
        assert "unsafe-inline" in page.headers["content-security-policy"]
        assert "script-src 'self'" in script.headers["content-security-policy"]
        assert "unsafe-inline" not in script.headers["content-security-policy"].split("script-src", 1)[1].split(";", 1)[0]
        assert page.headers["x-content-type-options"] == "nosniff"
        assert page.headers["referrer-policy"] == "no-referrer"
        assert page.headers["cache-control"] == "no-store"
        assert "access-control-allow-origin" not in page.headers
        assert client.get("/admin/sensitive.txt").status_code == 404
        assert client.get("/admin/../../sensitive.txt").status_code in {404, 307}


def test_managed_library_assignment_and_stale_version_id_return_409(tmp_path):
    qdrant = QdrantClient(":memory:")
    manager = _service(tmp_path, qdrant)
    source = tmp_path / "managed.txt"
    source.write_text("amberwidget managed content", encoding="utf-8")
    job = manager.submit_import(
        source,
        display_name="managed.txt",
        library_id="tool-docs",
        library_name="tool-docs",
    )
    assert manager.run_job(job.job_id).status == "completed"
    active = manager.get_file(job.file_id)
    assert active.library_id == "tool-docs"
    app = create_app(tmp_path, manager=manager, admin_token="test-secret")
    try:
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer test-secret"}
            libraries = client.get("/api/admin/libraries", headers=headers)
            assert libraries.status_code == 200, libraries.text
            source_entry = next(
                item
                for library in libraries.json()["libraries"]
                if library["library_id"] == "tool-docs"
                for item in library["sources"]
                if item.get("file_id") == job.file_id
            )
            assert source_entry["version_id"] == active.active_version_id
            assert source_entry["manageable"] is True

            response = client.post(
                "/api/admin/search",
                headers=headers,
                json={
                    "library_id": "tool-docs",
                    "source_type": "managed",
                    "file_id": job.file_id,
                    "version": active.version,
                    "version_id": "0" * 32,
                    "query": "amberwidget",
                },
            )
            assert response.status_code == 409
            assert response.json()["error"] == "stale_snapshot"
            assert "刷新资料库清单" in response.json()["detail"]
            calls = client.get(
                "/api/admin/calls?type=http&retrieval_target=file:" + job.file_id,
                headers=headers,
            )
            assert calls.status_code == 200, calls.text
            assert calls.json()["total"] == 1
            call = calls.json()["items"][0]
            assert call["retrieval_target"] == f"file:{job.file_id}"
            assert "amberwidget" not in calls.text
    finally:
        manager.close()
        qdrant.close()


def test_managed_search_rejects_source_changed_during_retrieval(tmp_path, monkeypatch):
    qdrant = QdrantClient(":memory:")
    manager = _service(tmp_path, qdrant)
    source = tmp_path / "changing.txt"
    source.write_text("amberwidget original bytes", encoding="utf-8")
    job = manager.submit_import(source, display_name="changing.txt")
    assert manager.run_job(job.job_id).status == "completed"
    snapshot = manager.snapshot(job.file_id)

    def mutate_source(_service, _query, *, config):
        snapshot.source_path.write_text("tampered while search runs", encoding="utf-8")
        return {"evidence": []}

    monkeypatch.setattr("wenjin.retrieval.service.RetrievalService.search", mutate_source)
    try:
        with pytest.raises(StaleSnapshotError, match="原文在检索期间发生变化"):
            manager.search(job.file_id, "amberwidget", version_id=snapshot.version_id)
    finally:
        manager.close()
        qdrant.close()
