from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from wenjin.auth import BearerAuthMiddleware


async def headers(request: Request):
    return JSONResponse({"client": request.headers.get("x-wenjin-authenticated-client")})


def app():
    return Starlette(
        routes=[Route("/mcp", headers)],
        middleware=[Middleware(
            BearerAuthMiddleware,
            api_key="a" * 32,
            client_id="claude-code",
        )],
    )


def test_missing_or_wrong_bearer_is_rejected(capsys):
    client = TestClient(app())
    assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401
    output = capsys.readouterr().out
    assert '"event":"mcp_auth_denied"' in output
    assert "wrong" not in output


def test_valid_bearer_injects_trusted_client_and_overwrites_spoof():
    client = TestClient(app())
    response = client.get("/mcp", headers={
        "Authorization": f"Bearer {'a' * 32}",
        "X-Wenjin-Authenticated-Client": "attacker",
    })
    assert response.status_code == 200
    assert response.json() == {"client": "claude-code"}
