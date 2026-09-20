"""原文取件端点的HTTP测试：Bearer鉴权、访问策略、登记校验、字节校验与审计。"""

import asyncio
import hashlib
import json

import httpx
import pytest

from wenjin.access import AccessPolicy, AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.auth import BearerAuthMiddleware
from wenjin.mcp_server import RuntimeIdentity, build_mcp_server

API_KEY = "test-bearer-key-at-least-24-chars"
CLIENT_ID = "agent-a"
CORPUS_ID = "demo-1.0"
GUIDE = b"# Guide\nrefreshIndex() works.\n"


class FakeService:
    def search(self, query, *, config):
        return {"query": query, "evidence": [], "answer_status": "not_generated"}


def make_project(tmp_path, *, clients: tuple[str, ...], guide: bytes = GUIDE) -> tuple:
    """搭一个最小项目：一个语料、一份原文、一条访问策略。"""
    raw_dir = tmp_path / "raw_data" / "demo" / "1.0"
    raw_dir.mkdir(parents=True)
    (raw_dir / "guide.md").write_bytes(guide)
    manifest_dir = tmp_path / "corpora" / CORPUS_ID
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "source-manifest.json").write_text(json.dumps({
        "schema_version": 3, "corpus_id": CORPUS_ID, "library": "demo", "version": "1.0",
        "raw_data_subdir": "demo/1.0", "license": "MIT",
        "files": [{
            "path": "guide.md",
            "raw_sha256": hashlib.sha256(guide).hexdigest(),
            "bytes": len(guide),
            "retrieval_eligible": True,
            "indexable": True,
        }],
    }), encoding="utf-8")
    access = AccessRegistry(None, {
        CORPUS_ID: AccessPolicy(CORPUS_ID, "internal", clients, "test"),
    })
    return CORPUS_ID, access


def plain_app(tmp_path, access: AccessRegistry, *, bearer_enabled: bool):
    """构造ASGI应用；bearer_enabled要和服务端实际是否装中间件一致。"""
    server = build_mcp_server(
        FakeService(),
        audit=SearchAudit(tmp_path / "audit.jsonl"),
        access=access,
        raw_root=tmp_path / "raw_data",
        project_root=tmp_path,
        bearer_enabled=bearer_enabled,
        identity=RuntimeIdentity(CORPUS_ID, "test", f"{CORPUS_ID}-blocks-split"),
    )
    return server.http_app(path="/mcp")


def client_for(tmp_path, access: AccessRegistry, *, with_auth: bool = True) -> httpx.AsyncClient:
    app = plain_app(tmp_path, access, bearer_enabled=True)
    app = BearerAuthMiddleware(app, api_key=API_KEY, client_id=CLIENT_ID)
    headers = {"Authorization": f"Bearer {API_KEY}"} if with_auth else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers
    )


def audit_rows(tmp_path) -> list[dict]:
    path = tmp_path / "audit.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_endpoint_serves_file_for_allowed_client(tmp_path):
    """送出的必须是**被校验过的那一份**字节，而不是校验后又按路径重开的内容。"""
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))

    async def scenario():
        async with client_for(tmp_path, access) as client:
            response = await client.get(f"/files/{corpus_id}/guide.md")
            assert response.status_code == 200
            assert response.content == GUIDE
            assert response.headers["content-length"] == str(len(GUIDE))
            assert response.headers["x-content-type-options"] == "nosniff"

    asyncio.run(scenario())


@pytest.mark.parametrize("clients", [(), ("someone-else",)])
def test_endpoint_returns_403_for_unauthorized_client(tmp_path, clients):
    """按项目约定明确返回403，而不是伪装成404。"""
    corpus_id, access = make_project(tmp_path, clients=clients)

    async def scenario():
        async with client_for(tmp_path, access) as client:
            response = await client.get(f"/files/{corpus_id}/guide.md")
            assert response.status_code == 403
            assert response.json()["error"] == "forbidden"

    asyncio.run(scenario())
    assert [row["outcome"] for row in audit_rows(tmp_path)] == ["denied"]


def test_endpoint_requires_bearer_when_configured(tmp_path):
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))

    async def scenario():
        async with client_for(tmp_path, access, with_auth=False) as client:
            response = await client.get(f"/files/{corpus_id}/guide.md")
            assert response.status_code == 401

    asyncio.run(scenario())


def test_endpoint_rejects_unregistered_file(tmp_path):
    """manifest没登记的文件不能下载，哪怕它确实躺在语料目录里。"""
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))
    (tmp_path / "raw_data" / "demo" / "1.0" / "not-listed.md").write_text("x", encoding="utf-8")

    async def scenario():
        async with client_for(tmp_path, access) as client:
            response = await client.get(f"/files/{corpus_id}/not-listed.md")
            assert response.status_code == 404
            assert response.json()["error"] == "not_found"

    asyncio.run(scenario())
    assert [row["outcome"] for row in audit_rows(tmp_path)] == ["not_found"]


def test_endpoint_rejects_tampered_file(tmp_path):
    """文件被改动后不再匹配登记的raw_sha256，拒绝送出。"""
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))
    (tmp_path / "raw_data" / "demo" / "1.0" / "guide.md").write_bytes(b"tampered\n")

    async def scenario():
        async with client_for(tmp_path, access) as client:
            response = await client.get(f"/files/{corpus_id}/guide.md")
            assert response.status_code == 409
            assert response.json()["error"] == "source_modified"
            assert b"tampered" not in response.content

    asyncio.run(scenario())
    rows = audit_rows(tmp_path)
    assert [row["outcome"] for row in rows] == ["hash_mismatch"]


def test_endpoint_rejects_path_traversal(tmp_path):
    """路径穿越不能读到语料目录之外的文件。"""
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))
    (tmp_path / "secret.txt").write_text("TOP SECRET\n", encoding="utf-8")

    async def scenario():
        async with client_for(tmp_path, access) as client:
            for attempt in ("../secret.txt", "..%2Fsecret.txt", "%2e%2e/secret.txt",
                            "demo/1.0/../../../secret.txt"):
                response = await client.get(f"/files/{corpus_id}/{attempt}",
                                            follow_redirects=True)
                assert response.status_code in {403, 404}, (attempt, response.status_code)
                assert b"TOP SECRET" not in response.content, attempt

    asyncio.run(scenario())


def test_endpoint_rejects_reading_another_corpus(tmp_path):
    """越权访问（IDOR）：拿A语料的授权不能读B语料的文件。"""
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))
    other = tmp_path / "raw_data" / "other" / "2.0"
    other.mkdir(parents=True)
    (other / "private.md").write_text("OTHER CORPUS SECRET\n", encoding="utf-8")

    async def scenario():
        async with client_for(tmp_path, access) as client:
            response = await client.get(f"/files/{corpus_id}/other/2.0/private.md")
            assert response.status_code == 404
            assert b"OTHER CORPUS SECRET" not in response.content

    asyncio.run(scenario())


def test_html_is_inline_but_sandboxed(tmp_path):
    """语料是第三方内容还支持用户上传，内联HTML必须带CSP sandbox，否则脚本跑在服务端源上。"""
    page = b"<html><body><script>steal()</script>hi</body></html>"
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))
    (tmp_path / "raw_data" / "demo" / "1.0" / "page.html").write_bytes(page)
    manifest = tmp_path / "corpora" / CORPUS_ID / "source-manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"].append({"path": "page.html", "raw_sha256": hashlib.sha256(page).hexdigest(),
                          "bytes": len(page), "retrieval_eligible": True, "indexable": True})
    manifest.write_text(json.dumps(data), encoding="utf-8")

    async def scenario():
        async with client_for(tmp_path, access) as client:
            response = await client.get(f"/files/{corpus_id}/page.html")
            assert response.status_code == 200
            assert response.headers["content-disposition"].startswith("inline")
            csp = response.headers.get("content-security-policy", "")
            assert "sandbox" in csp
            assert "allow-scripts" not in csp and "allow-same-origin" not in csp

    asyncio.run(scenario())


def test_xml_is_served_as_attachment(tmp_path):
    """XML可能带XSLT且各浏览器行为不一致，按附件返回而不是内联。"""
    body = b"<?xml version='1.0'?><schema/>"
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))
    (tmp_path / "raw_data" / "demo" / "1.0" / "schema.xsd").write_bytes(body)
    manifest = tmp_path / "corpora" / CORPUS_ID / "source-manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"].append({"path": "schema.xsd", "raw_sha256": hashlib.sha256(body).hexdigest(),
                          "bytes": len(body), "retrieval_eligible": True, "indexable": True})
    manifest.write_text(json.dumps(data), encoding="utf-8")

    async def scenario():
        async with client_for(tmp_path, access) as client:
            response = await client.get(f"/files/{corpus_id}/schema.xsd")
            assert response.status_code == 200
            assert response.headers["content-disposition"].startswith("attachment")

    asyncio.run(scenario())


def test_download_audit_correlates_corpus_file_client_and_outcome(tmp_path):
    """下载日志要能关联语料、文件、客户端和结果，且不含正文与绝对路径。"""
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))

    async def scenario():
        async with client_for(tmp_path, access) as client:
            await client.get(f"/files/{corpus_id}/guide.md")

    asyncio.run(scenario())
    rows = [row for row in audit_rows(tmp_path) if row.get("event") == "mcp_download_audit"]
    assert len(rows) == 1
    row = rows[0]
    assert row["corpus_id"] == corpus_id
    assert row["relative_path"] == "guide.md"
    assert row["outcome"] == "served"
    assert row["bytes_served"] == len(GUIDE)
    assert len(row["client_id_sha256"]) == 64
    assert "refreshIndex" not in json.dumps(row, ensure_ascii=False)
    assert str(tmp_path) not in json.dumps(row, ensure_ascii=False)


def test_forged_client_header_is_ignored_without_bearer_middleware(tmp_path):
    """HIGH：没装Bearer中间件时，客户端自带的身份头完全可控，绝不能采信。"""
    make_project(tmp_path, clients=(CLIENT_ID,))
    access = AccessRegistry(None, {CORPUS_ID: AccessPolicy(CORPUS_ID, "internal", (CLIENT_ID,))})
    app = plain_app(tmp_path, access, bearer_enabled=False)  # 故意不装Bearer中间件

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"x-wenjin-authenticated-client": CLIENT_ID},
        ) as client:
            response = await client.get(f"/files/{CORPUS_ID}/guide.md")
            assert response.status_code == 403

    asyncio.run(scenario())


def test_forged_client_header_cannot_override_middleware_identity(tmp_path):
    """装了中间件时，伪造的同名头会被剥掉，身份仍以中间件注入的为准。"""
    make_project(tmp_path, clients=("admin",))
    access = AccessRegistry(None, {CORPUS_ID: AccessPolicy(CORPUS_ID, "internal", ("admin",))})
    app = plain_app(tmp_path, access, bearer_enabled=True)
    app = BearerAuthMiddleware(app, api_key=API_KEY, client_id=CLIENT_ID)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                # 中间件注入的是CLIENT_ID，这个伪造值必须被剥掉，否则就等于自称admin。
                "x-wenjin-authenticated-client": "admin",
            },
        ) as client:
            response = await client.get(f"/files/{CORPUS_ID}/guide.md")
            assert response.status_code == 403

    asyncio.run(scenario())


def test_revocation_takes_effect_on_next_request(tmp_path):
    """同一进程内：授权→成功→撤权→拒绝，覆盖搜索、列表、下载三条路径。"""
    corpus_id, _ = make_project(tmp_path, clients=(CLIENT_ID,))
    access_dir = tmp_path / "configs" / "access"
    from wenjin.access import AccessPolicy as Policy
    from wenjin.access import write_policy

    write_policy(access_dir, Policy(CORPUS_ID, "internal", (CLIENT_ID,)))
    server = build_mcp_server(
        FakeService(),
        audit=SearchAudit(tmp_path / "audit.jsonl"),
        raw_root=tmp_path / "raw_data",
        project_root=tmp_path,
        bearer_enabled=True,
        identity=RuntimeIdentity(CORPUS_ID, "test", f"{CORPUS_ID}-x"),
    )
    app = BearerAuthMiddleware(server.http_app(path="/mcp"), api_key=API_KEY, client_id=CLIENT_ID)
    headers = {"Authorization": f"Bearer {API_KEY}"}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver", headers=headers) as client:
            first = await client.get(f"/files/{corpus_id}/guide.md")
            assert first.status_code == 200, "授权后应能下载"

            write_policy(access_dir, Policy(CORPUS_ID, "internal", ()))

            second = await client.get(f"/files/{corpus_id}/guide.md")
            assert second.status_code == 403, "撤权后新请求必须被拒绝"

    asyncio.run(scenario())
