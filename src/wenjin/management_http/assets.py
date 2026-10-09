"""管理界面静态资源的白名单读取与重定向。

页面资源暂留在 wenjin.file_management 包中；此模块只负责服务这些静态文件。
"""

from __future__ import annotations

from importlib import resources

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

_ADMIN_ASSETS = {"index.html", "app.js", "identity.js", "styles.css"}
_ADMIN_SVG_ASSETS = {"favicon.svg", "logo.svg", "file-icon.svg"}

def _read_admin_asset(asset_name: str) -> bytes:
    return resources.files("wenjin.file_management").joinpath("admin", asset_name).read_bytes()

async def _admin_asset(request: Request):
    """仅从固定资源包和白名单读取文件，不拼接不可信路径。"""
    asset_name = request.path_params.get("asset_path", "") or "index.html"
    allowed = _ADMIN_ASSETS | _ADMIN_SVG_ASSETS
    if asset_name not in allowed:
        raise HTTPException(404, "静态资源不存在")
    content_type = {
        "index.html": "text/html; charset=utf-8",
        "app.js": "text/javascript; charset=utf-8",
        "identity.js": "text/javascript; charset=utf-8",
        "styles.css": "text/css; charset=utf-8",
        **{name: "image/svg+xml" for name in _ADMIN_SVG_ASSETS},
    }[asset_name]
    return Response(_read_admin_asset(asset_name), media_type=content_type)

async def _admin_root(request: Request):
    return RedirectResponse("/admin/", status_code=307)
