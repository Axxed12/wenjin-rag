"""本机账户、授权和身份审计 HTTP 接口。"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from wenjin.identity.policy import AuthenticationFailed
from wenjin.management_http.http_common import _is_loopback, _jsonable

SESSION_COOKIE = "wenjin_session"
CSRF_HEADER = "x-csrf-token"
MAX_LOGIN_BODY = 8192


def _store(request: Request):
    store = getattr(request.app.state, "identity_store", None)
    if store is None:
        raise HTTPException(404, "本地账户模式未启用")
    return store


async def mcp_identity_snapshot(request: Request):
    """给新版MCP桥接身份与最终可读资源；不返回grant表或其他账号信息。"""
    snapshot = getattr(request.state, "access_snapshot", None)
    if snapshot is None:
        # 老测试替身/兼容store没有原子snapshot API时仍重新验证当前Bearer。
        store = _store(request)
        supplied = request.headers.get("authorization", "")
        token = supplied.removeprefix("Bearer ") if supplied.startswith("Bearer ") else ""
        principal = await run_in_threadpool(store.resolve_mcp_credential, token)
        snapshot = await run_in_threadpool(store.load_access_snapshot, principal)
    principal = snapshot.principal
    resources = [
        {"resource_kind": kind, "resource_id": resource_id}
        for kind, resource_id in sorted(snapshot.active_resources)
        if kind != "managed_file" and snapshot.allows(kind, resource_id)
    ]
    manager = getattr(request.app.state, "manager", None)
    readable_managed_files = []
    if manager is not None:
        records = await run_in_threadpool(manager.list_files)
        for record in records:
            if (record.status == "active" and record.library_id and record.file_id
                    and snapshot.allows_managed_file(record.library_id, record.file_id)):
                readable_managed_files.append({
                    "library_id": record.library_id,
                    "file_id": record.file_id,
                })
    return JSONResponse({
        "schema_version": 1,
        "principal": {
            "user_id": principal.user_id,
            "username": principal.username,
            "role": principal.role,
            "revision": principal.revision,
            "must_change_password": principal.must_change_password,
        },
        "access": {
            "readable_resources": resources,
            "readable_managed_files": readable_managed_files,
        },
    }, headers={"Cache-Control": "no-store"})


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin", "")
    try:
        parsed = urlsplit(origin)
    except ValueError as exc:
        raise HTTPException(403, "Origin无效") from exc
    if (parsed.scheme != request.url.scheme
            or parsed.netloc.lower() != request.headers.get("host", "").lower()):
        raise HTTPException(403, "仅允许同源请求")


async def _payload(request: Request, *, max_bytes: int = 16_384) -> dict:
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > max_bytes:
                raise HTTPException(413, "请求体过大")
        except ValueError as exc:
            raise HTTPException(400, "Content-Length无效") from exc
    raw = await request.body()
    if len(raw) > max_bytes:
        raise HTTPException(413, "请求体过大")
    try:
        payload = json.loads(raw or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "请求体必须是JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求体必须是JSON对象")
    return payload


def _revision(payload: dict) -> int:
    value = payload.get("expected_revision")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HTTPException(422, "expected_revision必须是非负整数")
    return value


async def mode(request: Request):
    store = getattr(request.app.state, "identity_store", None)
    if store is None:
        return JSONResponse({"mode": "legacy", "authentication_required": bool(request.app.state.admin_token)})
    is_open = await run_in_threadpool(store.bootstrap_is_open)
    return JSONResponse({
        "mode": "accounts", "bootstrap_open": is_open,
        "session_policy": {
            "idle_seconds": store.session_idle_seconds,
            "absolute_seconds": store.session_absolute_seconds,
        },
    })


async def login(request: Request):
    store = _store(request)
    _require_same_origin(request)
    now = time.monotonic()
    remote = request.client.host if request.client else "unknown"
    attempts = request.app.state.login_attempts
    recent = [stamp for stamp in attempts.get(remote, []) if now - stamp < 60]
    if len(recent) >= 8:
        attempts[remote] = recent
        raise HTTPException(429, "登录尝试过于频繁，请稍后重试")
    recent.append(now)
    attempts[remote] = recent
    payload = await _payload(request, max_bytes=MAX_LOGIN_BODY)
    username, password = payload.get("username"), payload.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or len(username) > 64 or len(password) > 1024:
        raise HTTPException(422, "用户名或密码格式无效")
    try:
        session = await run_in_threadpool(store.login, username, password)
    except AuthenticationFailed as exc:
        raise HTTPException(401, "用户名或密码错误") from exc
    attempts.pop(remote, None)
    response = JSONResponse({"user": _jsonable(session.principal), "csrf_token": session.csrf_token,
                             "expires_at": session.expires_at,
                             "must_change_password": session.must_change_password})
    response.set_cookie(SESSION_COOKIE, session.token, httponly=True, secure=request.url.scheme == "https",
                        samesite="strict", path="/", max_age=request.app.state.session_absolute_seconds)
    return response


async def bootstrap(request: Request):
    store = _store(request)
    _require_same_origin(request)
    if not request.client or not _is_loopback(request.client.host):
        raise HTTPException(403, "首位管理员只能从loopback初始化")
    expected = request.app.state.admin_token
    supplied = request.headers.get("authorization", "")
    candidate = supplied.removeprefix("Bearer ") if supplied.startswith("Bearer ") else ""
    if not expected or not candidate or not __import__("hmac").compare_digest(candidate, expected):
        raise HTTPException(401, "需要初始化用管理Bearer token")
    payload = await _payload(request, max_bytes=MAX_LOGIN_BODY)
    username, password = payload.get("username"), payload.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or len(password) > 1024:
        raise HTTPException(422, "用户名或密码格式无效")
    user = await run_in_threadpool(store.bootstrap_first_admin, username, password)
    return JSONResponse({"user": _jsonable(user), "bootstrap_open": False}, status_code=201)


async def me(request: Request):
    if getattr(request.app.state, "identity_store", None) is None:
        raise HTTPException(404, "本地账户模式未启用")
    principal = request.state.principal
    return JSONResponse({"user": _jsonable(principal), "mode": "accounts"})


async def csrf_refresh(request: Request):
    origin = request.headers.get("origin", "")
    try:
        parsed = urlsplit(origin)
    except ValueError as exc:
        raise HTTPException(403, "Origin无效") from exc
    if (parsed.scheme != request.url.scheme
            or parsed.netloc.lower() != request.headers.get("host", "").lower()):
        raise HTTPException(403, "CSRF刷新仅允许同源请求")
    token = request.cookies.get(SESSION_COOKIE, "")
    csrf = await run_in_threadpool(_store(request).rotate_csrf, token)
    return JSONResponse({"csrf_token": csrf})


async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await run_in_threadpool(_store(request).logout, token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict",
                           secure=request.url.scheme == "https")
    return response


def _check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        raise HTTPException(403, "状态变更请求必须包含同源Origin")
    try:
        parsed = urlsplit(origin)
    except ValueError as exc:
        raise HTTPException(403, "Origin无效") from exc
    if parsed.scheme != request.url.scheme or parsed.netloc.lower() != request.headers.get("host", "").lower():
        raise HTTPException(403, "跨源状态变更已拒绝")
    csrf = request.headers.get(CSRF_HEADER, "")
    token = request.cookies.get(SESSION_COOKIE, "")
    if not csrf or not token or not _store(request).verify_csrf(token, csrf):
        raise HTTPException(403, "CSRF校验失败")


async def change_password(request: Request):
    _check_origin(request)
    payload = await _payload(request, max_bytes=MAX_LOGIN_BODY)
    old_password, new_password = payload.get("old_password"), payload.get("new_password")
    if not isinstance(old_password, str) or not isinstance(new_password, str) or len(old_password) > 1024 or len(new_password) > 1024:
        raise HTTPException(422, "密码格式无效")
    try:
        user = await run_in_threadpool(_store(request).change_password, request.cookies.get(SESSION_COOKIE),
                                       old_password, new_password)
    except AuthenticationFailed as exc:
        from wenjin.identity.policy import CurrentPasswordIncorrect
        if isinstance(exc, CurrentPasswordIncorrect):
            return JSONResponse(
                {"error": "current_password_invalid", "detail": "当前密码不正确，请重新输入。"},
                status_code=422,
            )
        raise
    response = JSONResponse({"user": _jsonable(user), "must_change_password": False})
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict",
                           secure=request.url.scheme == "https")
    return response


async def users(request: Request):
    store, actor = _store(request), request.state.principal
    if request.method == "GET":
        return JSONResponse({"users": _jsonable(await run_in_threadpool(store.list_users, actor))})
    payload = await _payload(request)
    username, password = payload.get("username"), payload.get("password")
    role = payload.get("role", "reader")
    if not isinstance(username, str) or not isinstance(password, str) or len(password) > 1024:
        raise HTTPException(422, "用户名或临时密码格式无效")
    user = await run_in_threadpool(store.create_user, actor, username, password, role=role)
    return JSONResponse({"user": _jsonable(user)}, status_code=201)


async def update_user(request: Request):
    store, actor = _store(request), request.state.principal
    user_id = request.path_params["user_id"]
    payload = await _payload(request)
    revision = _revision(payload)
    if "username" in payload:
        user = await run_in_threadpool(store.rename_user, actor, user_id, payload["username"], expected_revision=revision)
    elif "role" in payload:
        user = await run_in_threadpool(store.set_user_role, actor, user_id, payload["role"], expected_revision=revision)
    elif "active" in payload:
        user = await run_in_threadpool(store.set_user_active, actor, user_id, payload["active"], expected_revision=revision)
    else:
        raise HTTPException(422, "只支持修改username、role或active")
    return JSONResponse({"user": _jsonable(user)})


async def reset_password(request: Request):
    payload = await _payload(request, max_bytes=MAX_LOGIN_BODY)
    password = payload.get("new_password")
    if not isinstance(password, str) or len(password) > 1024:
        raise HTTPException(422, "新密码格式无效")
    user = await run_in_threadpool(_store(request).reset_password, request.state.principal,
                                   request.path_params["user_id"], password,
                                   expected_revision=_revision(payload))
    return JSONResponse({"user": _jsonable(user)})


async def resources(request: Request):
    await synchronize_resources(request)
    store, actor = _store(request), request.state.principal
    resource_rows = await run_in_threadpool(store.list_resources, actor)
    grants = await run_in_threadpool(store.list_grants, actor)
    users_list = await run_in_threadpool(store.list_users, actor)
    library_names = {
        item["library_id"]: item["display_name"]
        for item in await run_in_threadpool(request.app.state.manager.list_libraries)
    }
    resource_rows = [
        {
            **item,
            "display_name": (
                library_names.get(item["resource_id"], item["resource_id"])
                if item["resource_kind"] == "library" else item["resource_id"]
            ),
        }
        for item in resource_rows
    ]
    return JSONResponse({"resources": resource_rows, "grants": _jsonable(grants), "users": _jsonable(users_list)})


async def synchronize_resources(request: Request):
    """从当前真实目录重建活动稳定资源视图；不授予任何主体权限。"""
    store = _store(request)
    manager = request.app.state.manager
    files = await run_in_threadpool(manager.list_files)
    from wenjin.management_http.search import _legacy_service

    catalog = await run_in_threadpool(_legacy_service(request).catalog, files)
    active: set[tuple[str, str]] = set()
    for library in catalog.get("libraries", []):
        library_id = library.get("library_id")
        if not isinstance(library_id, str):
            continue
        has_active_source = False
        for source in library.get("sources", []):
            if source.get("source_type") == "managed":
                file_id = source.get("file_id")
                if isinstance(file_id, str):
                    active.add(("managed_file", file_id))
                    has_active_source = True
            elif source.get("is_active") and source.get("searchable"):
                has_active_source = True
        if has_active_source:
            active.add(("library", library_id))
    for library in await run_in_threadpool(manager.list_libraries):
        active.add(("library", library["library_id"]))
    await run_in_threadpool(store.synchronize_resource_catalog, active)


def _source_visibility(library: dict) -> str:
    values = set()
    for source in library.get("sources", []):
        raw = source.get("source_visibility", source.get("visibility"))
        if raw == "public":
            values.add("public")
        elif raw in {"private", "internal"}:
            values.add("private")
    if not values:
        return "unknown"
    return next(iter(values)) if len(values) == 1 else "mixed"


async def _classification_view(store, library: dict) -> dict:
    library_id = library["library_id"]
    source_visibility = _source_visibility(library)
    saved = await run_in_threadpool(store.get_library_classification, library_id)
    if saved is None:
        value = "public" if source_visibility == "public" else "private"
        revision, defaulted = 0, True
    else:
        value = saved["classification"]
        revision, defaulted = saved["revision"], False
    return {
        "classification": value,
        "revision": revision,
        "defaulted": defaulted,
        "source_visibility": source_visibility,
    }


async def _attach_classifications(request: Request, catalog: dict) -> dict:
    store = getattr(request.app.state, "identity_store", None)
    if store is None:
        return catalog
    libraries = []
    for library in catalog.get("libraries", []):
        libraries.append({
            **library,
            "classification": await _classification_view(store, library),
            "source_visibility": _source_visibility(library),
        })
    return {**catalog, "libraries": libraries}


async def visible_catalog(
    request: Request, catalog: dict, *, admin_discovery: bool = False
) -> dict:
    store = getattr(request.app.state, "identity_store", None)
    if store is None:
        return catalog
    principal = request.state.principal
    # 管理员目录用于资料库管理；实际读取权限由当前IdentityStore快照决定。
    admin_discovery = admin_discovery and principal.role == "admin"
    output = []
    for library in catalog.get("libraries", []):
        library_id = library.get("library_id")
        visible_sources = []
        readable_library = await run_in_threadpool(
            store.can_read, principal, "library", library_id
        )
        for source in library.get("sources", []):
            if source.get("source_type") == "managed":
                allowed = await run_in_threadpool(
                    store.can_read_managed_file, principal, library_id, source.get("file_id", "")
                )
            else:
                if not source.get("is_active") and not admin_discovery:
                    continue
                allowed = readable_library
                if source.get("source_scope") == "private_candidate" and not admin_discovery:
                    continue
            if allowed or admin_discovery:
                visible_sources.append({
                    **source,
                    "readable": allowed,
                    "searchable": bool(source.get("searchable") and allowed),
                })
        if visible_sources or readable_library or admin_discovery:
            output.append({
                **library,
                "readable": readable_library,
                "sources": visible_sources,
            })
    return {**catalog, "libraries": output}


async def authorized_libraries(request: Request):
    if getattr(request.app.state, "identity_store", None) is not None:
        await synchronize_resources(request)
    from wenjin.management_http.search import _admin_libraries

    response = await _admin_libraries(request)
    if getattr(request.app.state, "identity_store", None) is None:
        return response
    catalog = await _attach_classifications(request, json.loads(response.body))
    visible = await visible_catalog(request, catalog, admin_discovery=True)
    known = {item.get("library_id") for item in visible.get("libraries", [])}
    for library in await run_in_threadpool(request.app.state.manager.list_libraries):
        library_id = library["library_id"]
        if library_id in known:
            continue
        readable = await run_in_threadpool(
            _store(request).can_read, request.state.principal, "library", library_id
        )
        if readable or request.state.principal.role == "admin":
            visible.setdefault("libraries", []).append({
                "library_id": library_id,
                "library": library["display_name"],
                "sources": [],
                "source_count": 0,
                "managed_library": True,
                "readable": readable,
                "classification": await _classification_view(_store(request), library),
                "source_visibility": _source_visibility(library),
            })
    return JSONResponse(visible)


async def library_classification(request: Request):
    store, actor = _store(request), request.state.principal
    library_id = request.path_params["library_id"]
    from wenjin.management_http.search import _admin_libraries

    current = await _admin_libraries(request)
    catalog = json.loads(current.body)
    library = next(
        (item for item in catalog.get("libraries", []) if item.get("library_id") == library_id),
        None,
    )
    if library is None:
        raise HTTPException(404, "资料库不存在")

    if request.method == "GET":
        view = await _classification_view(store, library)
        return JSONResponse({"classification": {"library_id": library_id, **view}})

    payload = await _payload(request)
    value = payload.get("classification")
    if not isinstance(value, str) or value not in {"public", "private"}:
        raise HTTPException(422, "classification必须是public或private")
    saved = await run_in_threadpool(
        store.set_library_classification,
        actor,
        library_id=library_id,
        classification=value,
        expected_revision=_revision(payload),
    )
    return JSONResponse({"classification": {
        "library_id": library_id,
        "classification": saved["classification"],
        "revision": saved["revision"],
        "defaulted": False,
        "source_visibility": _source_visibility(library),
    }})


async def authorized_search(request: Request):
    if getattr(request.app.state, "identity_store", None) is None:
        from wenjin.management_http.search import _admin_search

        return await _admin_search(request)
    payload = await _payload(request, max_bytes=32_768)
    library_id, source_type = payload.get("library_id"), payload.get("source_type")
    if not isinstance(library_id, str):
        raise HTTPException(422, "library_id必须是字符串")
    await synchronize_resources(request)
    if source_type == "managed":
        file_id = payload.get("file_id")
        if not isinstance(file_id, str):
            raise HTTPException(422, "managed检索必须提供file_id")
        store = _store(request)
        allowed = await run_in_threadpool(store.can_read_managed_file, request.state.principal, library_id, file_id)
        if not allowed:
            raise HTTPException(403, "当前账号没有该受管文件的读取授权")
    elif not await run_in_threadpool(_store(request).can_read, request.state.principal, "library", library_id):
        raise HTTPException(403, "当前账号没有该资料库的读取授权")
    from wenjin.management_http.search import _admin_search

    request._body = json.dumps(payload).encode("utf-8")
    return await _admin_search(request)


async def authorized_managed_search(request: Request):
    if getattr(request.app.state, "identity_store", None) is not None:
        payload = await _payload(request, max_bytes=32_768)
        file_id = payload.get("file_id")
        if not isinstance(file_id, str) or file_id.startswith("legacy:"):
            raise HTTPException(403, "当前入口只允许查询已授权的受管文件")
        record = await run_in_threadpool(request.app.state.manager.get_file, file_id)
        await _sync_managed_record(request, record)
        await _authorize_library(request, record.library_id, file_id)
        request._body = json.dumps(payload).encode("utf-8")
    from wenjin.management_http.search import _search

    return await _search(request)


async def authorized_download(request: Request):
    if getattr(request.app.state, "identity_store", None) is not None:
        file_id = request.path_params["file_id"]
        if file_id.startswith("legacy:"):
            raise HTTPException(403, "当前入口不允许读取未授权的legacy资源")
        record = await run_in_threadpool(request.app.state.manager.get_file, file_id)
        await _sync_managed_record(request, record)
        await _authorize_library(request, record.library_id, file_id)
    from wenjin.management_http.files import _file_download

    return await _file_download(request)


async def grants(request: Request):
    store, actor = _store(request), request.state.principal
    payload = await _payload(request)
    user_id = payload.get("user_id")
    resource_kind, resource_id = payload.get("resource_kind"), payload.get("resource_id")
    allowed = payload.get("allowed")
    if not all(isinstance(value, str) for value in (user_id, resource_kind, resource_id)) or not isinstance(allowed, bool):
        raise HTTPException(422, "授权参数无效")
    grant = await run_in_threadpool(store.set_grant, actor, resource_kind=resource_kind,
                                    resource_id=resource_id, user_id=user_id,
                                    allowed=allowed, expected_revision=_revision(payload))
    return JSONResponse({"grant": _jsonable(grant)})


async def audit(request: Request):
    limit_raw = request.query_params.get("limit", "100")
    if not limit_raw.isdigit():
        raise HTTPException(422, "limit必须是整数")
    items = await run_in_threadpool(_store(request).list_audit, request.state.principal,
                                    limit=int(limit_raw), before_event_id=request.query_params.get("before"))
    return JSONResponse({"events": items})


async def credentials(request: Request):
    store, actor = _store(request), request.state.principal
    if request.method == "GET":
        return JSONResponse({"credentials": await run_in_threadpool(store.list_mcp_credentials, actor)})
    payload = await _payload(request)
    credential = await run_in_threadpool(store.issue_mcp_credential, actor,
                                         label=payload.get("label", ""),
                                         expires_in_seconds=payload.get("expires_in_seconds"))
    return JSONResponse({"credential": _jsonable(credential)}, status_code=201)


async def admin_credentials(request: Request):
    store, actor = _store(request), request.state.principal
    user_id = request.path_params["user_id"]
    if request.method == "GET":
        return JSONResponse({"credentials": await run_in_threadpool(store.list_mcp_credentials, actor, user_id=user_id)})
    payload = await _payload(request)
    credential = await run_in_threadpool(
        store.issue_mcp_credential, actor, user_id=user_id, label=payload.get("label", ""),
        expires_in_seconds=payload.get("expires_in_seconds"),
    )
    return JSONResponse({"credential": _jsonable(credential)}, status_code=201)


async def revoke_credential(request: Request):
    await run_in_threadpool(_store(request).revoke_mcp_credential, request.state.principal,
                            request.path_params["credential_id"])
    return JSONResponse({"ok": True})


async def mcp_libraries(request: Request):
    from wenjin.management_http.search import _legacy_service

    catalog = await run_in_threadpool(_legacy_service(request).catalog,
                                      await run_in_threadpool(request.app.state.manager.list_files))
    if getattr(request.app.state, "identity_store", None) is None:
        return JSONResponse({"libraries": catalog.get("libraries", [])})
    await synchronize_resources(request)
    return JSONResponse(await visible_catalog(request, catalog))


async def _authorize_library(request: Request, library_id: str, file_id: str | None = None) -> None:
    store = getattr(request.app.state, "identity_store", None)
    if store is None:
        return
    principal = request.state.principal
    # 每个操作都在当前请求读取最新快照，撤权不依赖缓存失效。
    if file_id:
        if await run_in_threadpool(store.can_read_managed_file, principal, library_id, file_id):
            return
    elif await run_in_threadpool(store.can_read, principal, "library", library_id):
        return
    raise HTTPException(403, "当前账号没有该资料库的读取授权")


async def _sync_managed_record(request: Request, record) -> None:
    store = getattr(request.app.state, "identity_store", None)
    if store is None:
        return
    library_id = record.library_id or "managed-files"
    if record.status == "active":
        await run_in_threadpool(store.synchronize_resource, "library", library_id, active=True)
    await run_in_threadpool(store.synchronize_resource, "managed_file", record.file_id,
                            active=record.status == "active")


async def mcp_search(request: Request):
    payload = await _payload(request, max_bytes=32_768)
    library_id = payload.get("library_id")
    file_id = payload.get("file_id")
    if getattr(request.app.state, "identity_store", None) is not None and isinstance(file_id, str):
        # MCP个人桥接的managed简洁合同：工具只提交file_id和查询参数；
        # 归属与active version必须由服务端实时解析，不能由客户端冒充。
        record = await run_in_threadpool(request.app.state.manager.get_file, file_id)
        library_id = record.library_id or "managed-files"
        requested_version = payload.get("version_id")
        if record.status != "active" or not record.active_version_id:
            raise HTTPException(409, "受管文件当前没有可检索的活动版本")
        if requested_version is not None and requested_version != record.active_version_id:
            raise HTTPException(409, "受管文件版本已变化，请刷新资料库清单后重试")
        await _sync_managed_record(request, record)
        # Ignore a caller-selected library_id and bind both authorization and search to
        # the current managed record, so a grant on another library cannot be borrowed.
        payload.update({
            "library_id": library_id,
            "source_type": "managed",
            "version_id": record.active_version_id,
        })
        request.state.managed_authorized_library_id = library_id
    if not isinstance(library_id, str):
        raise HTTPException(422, "library_id必须是字符串")
    if getattr(request.app.state, "identity_store", None) is not None:
        await synchronize_resources(request)
        await _authorize_library(request, library_id, file_id if isinstance(file_id, str) else None)
        if isinstance(file_id, str) and payload.get("source_type") == "managed":
            request._body = json.dumps(payload).encode("utf-8")
            from wenjin.management_http.search import _search

            return await _search(request)
    # 复用现有真实检索handler，仅提供完整后端授权后再交给它。
    from wenjin.management_http.search import _admin_search

    request._body = json.dumps(payload).encode("utf-8")
    return await _admin_search(request)


async def mcp_download(request: Request):
    file_id = request.path_params["file_id"]
    version_id = request.query_params.get("version_id")
    record = await run_in_threadpool(request.app.state.manager.get_file, file_id)
    if getattr(request.app.state, "identity_store", None) is not None:
        if (not isinstance(version_id, str)
                or not re.fullmatch(r"[0-9a-f]{32,64}", version_id)):
            raise HTTPException(422, "MCP managed原文请求必须提供完整version_id")
        if record.status != "active" or not record.active_version_id:
            raise HTTPException(409, "受管文件当前没有可下载的活动版本")
        if version_id != record.active_version_id:
            raise HTTPException(409, "受管文件版本已变化，请刷新文件清单后重试")
        await _sync_managed_record(request, record)
        authorized_library_id = record.library_id or "managed-files"
        await _authorize_library(request, authorized_library_id, file_id)
        request.state.managed_authorized_library_id = authorized_library_id
    from wenjin.management_http.files import _file_download

    return await _file_download(request)


async def shared_source_download(request: Request):
    library_id = request.path_params["library_id"]
    if getattr(request.app.state, "identity_store", None) is not None:
        await synchronize_resources(request)
        await _authorize_library(request, library_id)
    query = request.query_params
    corpus_id, target_id = query.get("corpus_id"), query.get("index_target_id")
    version_id, document_id = query.get("version_id"), query.get("document_id")
    if not all(isinstance(value, str) and value for value in (corpus_id, target_id, version_id, document_id)):
        raise HTTPException(422, "必须提供corpus_id、index_target_id、version_id和document_id")
    from wenjin.management_http.search import _legacy_service
    service = _legacy_service(request)
    try:
        verified = await run_in_threadpool(
            service.read_source,
            library_id=library_id,
            corpus_id=corpus_id,
            target_id=target_id,
            version_id=version_id,
            document_id=document_id,
        )
    except Exception as exc:
        from wenjin.retrieval.errors import StaleSnapshotError
        from wenjin.serving import SourceModified, SourceNotFound, SourceTooLarge, SourceUnverified

        if isinstance(exc, StaleSnapshotError):
            raise HTTPException(409, "资料库版本已变化，请刷新后重试") from exc
        if isinstance(exc, SourceTooLarge):
            raise HTTPException(413, "原文超过单次取件上限") from exc
        if isinstance(exc, (SourceModified, SourceUnverified)):
            raise HTTPException(409, "原文完整性校验未通过，拒绝返回内容") from exc
        if isinstance(exc, (SourceNotFound, ValueError)):
            raise HTTPException(404, "来源不存在") from exc
        raise
    return Response(verified.content, headers=verified.response_headers())


async def authorized_shared_source(request: Request):
    if getattr(request.app.state, "identity_store", None) is None:
        raise HTTPException(404, "共享原文取件仅在本地账户模式提供")
    return await shared_source_download(request)


async def mcp_file_list(request: Request):
    records = await run_in_threadpool(request.app.state.manager.list_files)
    if getattr(request.app.state, "identity_store", None) is None:
        return JSONResponse({"files": [_jsonable(item) for item in records]})
    active = [item for item in records if item.managed and item.status == "active"]
    store = request.app.state.identity_store
    for item in active:
        library_id = item.library_id or "managed-files"
        await run_in_threadpool(store.synchronize_resource, "library", library_id, active=True)
        await run_in_threadpool(store.synchronize_resource, "managed_file", item.file_id, active=True)
    allowed = []
    for item in active:
        library_id = item.library_id or "managed-files"
        allowed_file = await run_in_threadpool(store.can_read_managed_file, request.state.principal,
                                               library_id, item.file_id)
        if allowed_file:
            allowed.append({
                "file_id": item.file_id,
                "library_id": library_id,
                "managed": True,
                "status": item.status,
                "display_name": item.display_name,
                "version": item.version,
                "version_id": item.active_version_id,
                "active_version_id": item.active_version_id,
                "chunk_count": item.chunk_count,
                "updated_at": item.updated_at,
            })
    return JSONResponse({"files": allowed})
