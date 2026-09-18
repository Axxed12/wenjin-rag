"""原文取件端点的HTTP测试：Bearer鉴权、访问策略、路径安全和内容返回。"""

import asyncio
import json

import httpx
import pytest

from wenjin.access import AccessPolicy, AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.auth import BearerAuthMiddleware
from wenjin.mcp_server import build_mcp_server

API_KEY = "test-bearer-key-at-least-24-chars"
CLIENT_ID = "agent-a"


class FakeService:
    def search(self, query, *, config):
        return {"query": query, "evidence": [], "answer_status": "not_generated"}


FILE_URL = "/files/demo-1.0/demo/1.0/guide.md"  # 路径是raw_data相对的，语料ID只用于鉴权


def make_project(tmp_path, *, clients: tuple[str, ...]) -> tuple:
    """搭一个最小项目：一个语料、一份原文、一条访问策略。"""
    corpus_id = "demo-1.0"
    raw_dir = tmp_path / "raw_data" / "demo" / "1.0"
    raw_dir.mkdir(parents=True)
    (raw_dir / "guide.md").write_text("# Guide\nrefreshIndex() works.\n", encoding="utf-8")
    manifest_dir = tmp_path / "corpora" / corpus_id
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "source-manifest.json").write_text(json.dumps({
        "schema_version": 3, "corpus_id": corpus_id, "library": "demo", "version": "1.0",
        "raw_data_subdir": "demo/1.0", "license": "MIT",
        "files": [{"path": "guide.md", "retrieval_eligible": True}],
    }), encoding="utf-8")
    access = AccessRegistry(None, {
        corpus_id: AccessPolicy(corpus_id, "internal", clients, "test"),
    })
    return corpus_id, access


def plain_app(tmp_path, access: AccessRegistry, *, bearer_enabled: bool):
    """构造ASGI应用；bearer_enabled要和服务端实际是否装中间件一致。"""
    server = build_mcp_server(
        FakeService(),
        audit=SearchAudit(tmp_path / "audit.jsonl"),
        access=access,
        raw_root=tmp_path / "raw_data",
        project_root=tmp_path,
        bearer_enabled=bearer_enabled,
    )
    return server.http_app(path="/mcp")


def client_for(tmp_path, access: AccessRegistry, *, with_auth: bool = True) -> httpx.AsyncClient:
    app = plain_app(tmp_path, access, bearer_enabled=True)
    app = BearerAuthMiddleware(app, api_key=API_KEY, client_id=CLIENT_ID)
    headers = {"Authorization": f"Bearer {API_KEY}"} if with_auth else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=headers
    )


def test_endpoint_serves_file_for_allowed_client(tmp_path):
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))

    async def scenario():
        async with client_for(tmp_path, access) as client:
            response = await client.get(FILE_URL)
            assert response.status_code == 200
            assert "refreshIndex()" in response.text
            assert response.headers["x-content-type-options"] == "nosniff"

    asyncio.run(scenario())


@pytest.mark.parametrize("clients", [(), ("someone-else",)])
def test_endpoint_returns_403_for_unauthorized_client(tmp_path, clients):
    """按项目约定明确返回403，而不是伪装成404。"""
    corpus_id, access = make_project(tmp_path, clients=clients)

    async def scenario():
        async with client_for(tmp_path, access) as client:
            response = await client.get(FILE_URL)
            assert response.status_code == 403
            assert response.json()["error"] == "forbidden"

    asyncio.run(scenario())


def test_endpoint_requires_bearer_when_configured(tmp_path):
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))

    async def scenario():
        async with client_for(tmp_path, access, with_auth=False) as client:
            response = await client.get(FILE_URL)
            assert response.status_code == 401

    asyncio.run(scenario())


def test_endpoint_rejects_path_traversal(tmp_path):
    """路径穿越不能读到语料目录之外的文件。

    明文`..`会被框架在进入路由前归一化掉（返回307重定向），重定向后落到的
    corpus_id不是已知语料，因此被默认拒绝成403；百分号编码的`..`则被
    paths.resolve_within直接挡成404。这里断言的是安全性质本身：
    无论哪条路径，都拿不到200，也拿不到语料目录外的内容。
    """
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))
    (tmp_path / "secret.txt").write_text("TOP SECRET\n", encoding="utf-8")

    async def scenario():
        async with client_for(tmp_path, access) as client:
            for attempt in ("../secret.txt", "..%2Fsecret.txt", "%2e%2e/secret.txt",
                            "demo/1.0/../../../secret.txt"):
                response = await client.get(f"/files/{corpus_id}/{attempt}",
                                            follow_redirects=True)
                assert response.status_code in {403, 404}, (attempt, response.status_code)
                assert "TOP SECRET" not in response.text, attempt

    asyncio.run(scenario())


def test_endpoint_rejects_reading_another_corpus(tmp_path):
    """越权访问（IDOR）：取件路径是raw_data相对的，拿A语料的授权不能读B语料的文件。"""
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))
    other = tmp_path / "raw_data" / "other" / "2.0"
    other.mkdir(parents=True)
    (other / "private.md").write_text("OTHER CORPUS SECRET\n", encoding="utf-8")

    async def scenario():
        async with client_for(tmp_path, access) as client:
            response = await client.get(f"/files/{corpus_id}/other/2.0/private.md")
            assert response.status_code == 404
            assert "OTHER CORPUS SECRET" not in response.text

    asyncio.run(scenario())


def test_html_is_inline_but_sandboxed(tmp_path):
    """语料是第三方内容还支持用户上传，内联HTML必须带CSP sandbox，否则脚本跑在服务端源上。"""
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))
    (tmp_path / "raw_data" / "demo" / "1.0" / "page.html").write_text(
        "<html><body><script>steal()</script>hi</body></html>", encoding="utf-8"
    )

    async def scenario():
        async with client_for(tmp_path, access) as client:
            response = await client.get("/files/demo-1.0/demo/1.0/page.html")
            assert response.status_code == 200
            assert response.headers["content-disposition"].startswith("inline")
            csp = response.headers.get("content-security-policy", "")
            assert "sandbox" in csp
            assert "allow-scripts" not in csp and "allow-same-origin" not in csp
            assert response.headers["x-content-type-options"] == "nosniff"

    asyncio.run(scenario())


def test_xml_is_served_as_attachment(tmp_path):
    """XML可能带XSLT且各浏览器行为不一致，按附件返回而不是内联。"""
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))
    (tmp_path / "raw_data" / "demo" / "1.0" / "schema.xsd").write_text(
        "<?xml version='1.0'?><schema/>", encoding="utf-8"
    )

    async def scenario():
        async with client_for(tmp_path, access) as client:
            response = await client.get("/files/demo-1.0/demo/1.0/schema.xsd")
            assert response.status_code == 200
            assert response.headers["content-disposition"].startswith("attachment")
            assert "content-security-policy" not in response.headers

    asyncio.run(scenario())


def test_endpoint_returns_404_for_missing_file(tmp_path):
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))

    async def scenario():
        async with client_for(tmp_path, access) as client:
            response = await client.get(f"/files/{corpus_id}/demo/1.0/nope.md")
            assert response.status_code == 404
            assert response.json()["error"] == "not_found"

    asyncio.run(scenario())


def test_forged_client_header_is_ignored_without_bearer_middleware(tmp_path):
    """HIGH：没装Bearer中间件时，客户端自带的身份头完全可控，绝不能采信。

    不修的话，任何人发一个 x-wenjin-authenticated-client: <已知client_id> 就能读内部语料。
    """
    corpus_id, access = make_project(tmp_path, clients=(CLIENT_ID,))
    app = plain_app(tmp_path, access, bearer_enabled=False)  # 故意不装Bearer中间件

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"x-wenjin-authenticated-client": CLIENT_ID},
        ) as client:
            response = await client.get(FILE_URL)
            assert response.status_code == 403
            assert response.json()["error"] == "forbidden"

    asyncio.run(scenario())


def test_forged_client_header_cannot_override_middleware_identity(tmp_path):
    """装了中间件时，伪造的同名头会被剥掉，身份仍以中间件注入的为准。"""

    corpus_id, access = make_project(tmp_path, clients=("admin",))
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
            response = await client.get(FILE_URL)
            assert response.status_code == 403

    asyncio.run(scenario())
