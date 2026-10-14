"""管理后台 HTTP 应用装配与唯一服务资源生命周期。"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from wenjin.management_http.assets import _admin_asset, _admin_root
from wenjin.management_http.files import (
    _file_adopt,
    _file_delete,
    _file_download,
    _file_list,
    _file_replace,
    _file_resources,
    _file_status,
    _file_upload,
    _managed_library_create,
    _managed_library_list,
)
from wenjin.management_http.http_common import (
    DEFAULT_MAX_UPLOAD_BYTES,
    _guarded,
)
from wenjin.management_http.http_common import (
    validate_listen_security as validate_listen_security,
)
from wenjin.management_http.identity_http import (
    admin_credentials as _identity_admin_credentials,
)
from wenjin.management_http.identity_http import (
    audit as _identity_audit,
)
from wenjin.management_http.identity_http import (
    authorized_download as _authorized_download,
)
from wenjin.management_http.identity_http import (
    authorized_libraries as _authorized_libraries,
)
from wenjin.management_http.identity_http import (
    authorized_managed_search as _authorized_managed_search,
)
from wenjin.management_http.identity_http import (
    authorized_search as _authorized_search,
)
from wenjin.management_http.identity_http import (
    authorized_shared_source as _authorized_shared_source,
)
from wenjin.management_http.identity_http import (
    bootstrap as _identity_bootstrap,
)
from wenjin.management_http.identity_http import (
    change_password as _identity_change_password,
)
from wenjin.management_http.identity_http import (
    credentials as _identity_credentials,
)
from wenjin.management_http.identity_http import (
    csrf_refresh as _identity_csrf_refresh,
)
from wenjin.management_http.identity_http import (
    grants as _identity_grants,
)
from wenjin.management_http.identity_http import (
    library_classification as _identity_library_classification,
)
from wenjin.management_http.identity_http import (
    login as _identity_login,
)
from wenjin.management_http.identity_http import (
    logout as _identity_logout,
)
from wenjin.management_http.identity_http import (
    mcp_download as _mcp_download,
)
from wenjin.management_http.identity_http import (
    mcp_file_list as _mcp_file_list,
)
from wenjin.management_http.identity_http import (
    mcp_libraries as _mcp_libraries,
)
from wenjin.management_http.identity_http import (
    mcp_search as _mcp_search,
)
from wenjin.management_http.identity_http import (
    me as _identity_me,
)
from wenjin.management_http.identity_http import (
    mode as _identity_mode,
)
from wenjin.management_http.identity_http import (
    reset_password as _identity_reset_password,
)
from wenjin.management_http.identity_http import (
    resources as _identity_resources,
)
from wenjin.management_http.identity_http import (
    revoke_credential as _identity_revoke_credential,
)
from wenjin.management_http.identity_http import (
    shared_source_download as _mcp_shared_source_download,
)
from wenjin.management_http.identity_http import (
    update_user as _identity_update_user,
)
from wenjin.management_http.identity_http import (
    users as _identity_users,
)
from wenjin.management_http.jobs import _job_list, _job_retry, _job_status, _schedule_job
from wenjin.management_http.logs import _call_logs
from wenjin.management_http.releases import (
    initialize_release_jobs,
    release_routes,
    resume_release_jobs,
)
from wenjin.management_http.search import _admin_libraries, _admin_search, _search

_ADMIN_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


class _LazyEmbedder:
    """延迟创建本地模型适配器，使列表和状态请求无需加载模型。"""

    def __init__(self, settings, *, root: Path) -> None:
        self._settings = settings
        self._root = Path(root).resolve()
        self._instance = None
        self._lock = threading.Lock()

    def _load(self):
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    self._instance = self._settings.embedding(root=self._root)
        return self._instance

    def __getattr__(self, name):
        return getattr(self._load(), name)

async def _admin_config(request: Request):
    # 配置摘要无秘密字段，但API路由仍统一走Bearer保护。
    policy = request.app.state.search_policy
    identity_store = getattr(request.app.state, "identity_store", None)
    admin_token = getattr(request.app.state, "admin_token", None)
    authentication_mode = (
        "accounts" if identity_store is not None
        else "bearer" if admin_token
        else "none"
    )
    return JSONResponse({
        "max_upload_bytes": request.app.state.max_upload_bytes,
        "authentication_required": authentication_mode != "none",
        "authentication_mode": authentication_mode,
        "retrieval": policy.public_config(5),
    })


async def _health(request: Request):
    return JSONResponse({"status": "ready" if getattr(request.app.state, "manager", None) else "starting"})


_ENDPOINTS = {
    "health": _health,
    "files": _file_list,
    "file_status": _file_status,
    "file_resources": _file_resources,
    "upload": _file_upload,
    "replace": _file_replace,
    "adopt": _file_adopt,
    "job_status": _job_status,
    "job_list": _job_list,
    "admin_config": _admin_config,
    "admin_libraries": _admin_libraries,
    "admin_search": _admin_search,
    "call_logs": _call_logs,
    "retry": _job_retry,
    "delete": _file_delete,
    "search": _search,
    "download": _file_download,
}


def _route(handler, *, access: str = "admin"):
    async def endpoint(request: Request):
        return await _guarded(request, handler, access)

    return endpoint


def _routes():
    routes = [
        Route("/", _admin_root, methods=["GET"]),
        Route("/admin/", _admin_asset, methods=["GET"]),
        Route("/admin/{asset_path:path}", _admin_asset, methods=["GET"]),
        Route("/api/auth/mode", _route(_identity_mode, access="public"), methods=["GET"]),
        Route("/api/auth/login", _route(_identity_login, access="public"), methods=["POST"]),
        Route("/api/auth/bootstrap", _route(_identity_bootstrap, access="bootstrap"), methods=["POST"]),
        Route("/api/auth/me", _route(_identity_me, access="me"), methods=["GET"]),
        Route("/api/auth/csrf", _route(_identity_csrf_refresh, access="csrf_refresh"), methods=["POST"]),
        Route("/api/auth/logout", _route(_identity_logout, access="logout"), methods=["POST"]),
        Route("/api/auth/password", _route(_identity_change_password, access="change_password"), methods=["POST"]),
        Route("/api/auth/credentials", _route(_identity_credentials, access="reader"), methods=["GET", "POST"]),
        Route("/api/auth/credentials/{credential_id:str}", _route(_identity_revoke_credential, access="reader"), methods=["DELETE"]),
        Route("/api/admin/users", _route(_identity_users), methods=["GET", "POST"]),
        Route("/api/admin/users/{user_id:str}", _route(_identity_update_user), methods=["PATCH"]),
        Route("/api/admin/users/{user_id:str}/reset-password", _route(_identity_reset_password), methods=["POST"]),
        Route("/api/admin/users/{user_id:str}/credentials", _route(_identity_admin_credentials), methods=["GET", "POST"]),
        Route("/api/admin/credentials/{credential_id:str}", _route(_identity_revoke_credential), methods=["DELETE"]),
        Route("/api/admin/resources", _route(_identity_resources), methods=["GET"]),
        Route("/api/admin/grants", _route(_identity_grants), methods=["POST"]),
        Route("/api/admin/libraries/{library_id:str}/classification", _route(_identity_library_classification), methods=["GET", "PUT"]),
        Route("/api/admin/identity-audit", _route(_identity_audit), methods=["GET"]),
        Route("/api/mcp/libraries", _route(_mcp_libraries, access="mcp"), methods=["GET"]),
        Route("/api/mcp/files", _route(_mcp_file_list, access="mcp"), methods=["GET"]),
        Route("/api/mcp/search", _route(_mcp_search, access="mcp"), methods=["POST"]),
        Route("/api/mcp/files/{file_id:str}/source", _route(_mcp_download, access="mcp"), methods=["GET"]),
        Route("/api/mcp/libraries/{library_id:str}/source", _route(_mcp_shared_source_download, access="mcp"), methods=["GET"]),
        Route("/api/libraries/{library_id:str}/source", _route(_authorized_shared_source, access="reader"), methods=["GET"]),
        Route("/api/admin/config", _route(_ENDPOINTS["admin_config"]), methods=["GET"]),
        Route("/api/admin/libraries", _route(_authorized_libraries, access="reader"), methods=["GET"]),
        Route("/api/admin/search", _route(_authorized_search, access="reader"), methods=["POST"]),
        Route("/api/admin/calls", _route(_ENDPOINTS["call_logs"]), methods=["GET"]),
        Route("/api/health", _route(_ENDPOINTS["health"], access="public"), methods=["GET"]),
        Route("/api/files", _route(_ENDPOINTS["files"]), methods=["GET"]),
        Route("/api/admin/managed-libraries", _route(_managed_library_list), methods=["GET"]),
        Route("/api/admin/managed-libraries", _route(_managed_library_create), methods=["POST"]),
        Route("/api/files", _route(_ENDPOINTS["upload"]), methods=["POST"]),
        Route("/api/files/{file_id:str}", _route(_ENDPOINTS["file_status"]), methods=["GET"]),
        Route("/api/files/{file_id:str}", _route(_ENDPOINTS["replace"]), methods=["PUT"]),
        Route("/api/files/{file_id:str}", _route(_ENDPOINTS["delete"]), methods=["DELETE"]),
        Route("/api/files/{file_id:str}/adopt", _route(_ENDPOINTS["adopt"]), methods=["POST"]),
        Route("/api/files/{file_id:str}/resources", _route(_ENDPOINTS["file_resources"]), methods=["GET"]),
        Route("/api/files/{file_id:str}/download", _route(_authorized_download, access="reader"), methods=["GET"]),
        Route("/api/jobs", _route(_ENDPOINTS["job_list"]), methods=["GET"]),
        Route("/api/jobs/{job_id:str}", _route(_ENDPOINTS["job_status"]), methods=["GET"]),
        Route("/api/jobs/{job_id:str}/retry", _route(_ENDPOINTS["retry"]), methods=["POST"]),
        Route("/api/search", _route(_authorized_managed_search, access="reader"), methods=["POST"]),
    ]
    from wenjin.management_http.evaluations import evaluation_routes
    from wenjin.management_http.mcp_control import routes as mcp_control_routes

    return routes + mcp_control_routes(_route) + release_routes(_route) + evaluation_routes(_route)


def create_app(
    root: Path,
    *,
    admin_token: str | None = None,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    manager=None,
    settings=None,
    identity_db: Path | None = None,
    mcp_control_service=None,
) -> Starlette:
    """构造独立管理API；manager/settings仅供隔离验收注入。"""
    if max_upload_bytes < 1:
        raise ValueError("max_upload_bytes必须大于0")

    @asynccontextmanager
    async def lifespan(app: Starlette):
        initialize_release_jobs(app)
        try:
            if manager is None:
                from wenjin.file_management import FileManagementService
                from wenjin.qdrant_connection import create_qdrant_client
                from wenjin.settings import Settings

                project_root = Path(root).resolve()
                resolved_settings = settings or Settings.for_project_root(project_root)
                client = create_qdrant_client(
                    url=resolved_settings.qdrant_url,
                    api_key=resolved_settings.qdrant_api_key,
                )
                app.state.qdrant_client = client
                if getattr(resolved_settings, "model_provider", "local") == "remote":
                    from wenjin.models.providers import build_remote_model_bindings

                    bindings = build_remote_model_bindings(resolved_settings, root=project_root)
                    app.state.model_bindings = bindings
                    lazy_embedder = bindings.embedder
                    app.state.model_provider_identity = bindings.identity
                else:
                    app.state.model_bindings = None
                    app.state.model_provider_identity = None
                    lazy_embedder = _LazyEmbedder(resolved_settings, root=project_root)
                app.state.manager = FileManagementService(
                    root=project_root,
                    qdrant_client=client,
                    embedder=lazy_embedder,
                    embedding_model=resolved_settings.resolved_embedding_model_id,
                    managed_raw_data_root=resolved_settings.managed_raw_data_root,
                )
                app.state.settings = resolved_settings
                from wenjin.retrieval.lazy_reranker import build_shared_reranker
                from wenjin.retrieval.search_policy import SearchPolicy

                app.state.search_policy = SearchPolicy.from_settings(resolved_settings)
                app.state.shared_reranker = (
                    app.state.model_bindings.reranker
                    if app.state.model_bindings is not None
                    else build_shared_reranker(resolved_settings, root=project_root)
                )
            else:
                app.state.manager = manager
                app.state.qdrant_client = None
                app.state.settings = settings
                from wenjin.retrieval.search_policy import SearchPolicy

                app.state.search_policy = (
                    SearchPolicy.from_settings(settings)
                    if settings is not None else SearchPolicy(reranker_enabled=False)
                )
                app.state.shared_reranker = None
                app.state.model_bindings = None
                app.state.model_provider_identity = None
                if settings is not None and getattr(settings, "model_provider", "local") == "remote":
                    from wenjin.models.providers import build_remote_model_bindings

                    bindings = build_remote_model_bindings(settings, root=Path(root).resolve())
                    app.state.model_bindings = bindings
                    app.state.model_provider_identity = bindings.identity
                    app.state.shared_reranker = bindings.reranker
                    app.state.manager.embedder = bindings.embedder
                    if hasattr(app.state.manager, "index_adapter"):
                        app.state.manager.index_adapter.embedder = bindings.embedder
                    app.state.manager.embedding_model = settings.resolved_embedding_model_id
                elif settings is not None:
                    from wenjin.retrieval.lazy_reranker import build_shared_reranker

                    app.state.shared_reranker = build_shared_reranker(
                        settings, root=Path(root)
                    )
            pending = await run_in_threadpool(app.state.manager.resume_pending_jobs)
            for job in pending:
                await _schedule_job(app, job.job_id)
            if identity_db is not None:
                from wenjin.identity import IdentityStore

                identity_path = Path(identity_db).resolve()
                if not identity_path.is_absolute():
                    raise ValueError("identity_db必须解析为绝对路径")
                identity = IdentityStore(identity_path)
                await run_in_threadpool(identity.initialize)
                app.state.identity_store = identity
                app.state.session_absolute_seconds = identity.session_absolute_seconds
                from wenjin.evaluation_tasks.runtime import EvaluationRuntimeFactory
                from wenjin.evaluation_tasks.service import EvaluationJobStore

                task_store = EvaluationJobStore(
                    Path(root).resolve() / "data" / "evaluation-tasks.sqlite"
                )
                await run_in_threadpool(task_store.mark_interrupted)
                factory = EvaluationRuntimeFactory(root=Path(root), app_state=app.state)
                app.state.evaluation_job_store = task_store
                app.state.evaluation_runtime_factory = factory
                app.state.evaluation_service = factory.create_service(identity, task_store)
            await resume_release_jobs(app)
            yield
        finally:
            evaluation_service = getattr(app.state, "evaluation_service", None)
            if evaluation_service is not None:
                await evaluation_service.shutdown()
            tasks = list(app.state.running_jobs.values())
            tasks.extend(app.state.running_release_jobs.values())
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            reranker = getattr(app.state, "shared_reranker", None)
            close = getattr(getattr(app.state, "manager", None), "close", None)
            client = getattr(app.state, "qdrant_client", None)
            try:
                if reranker is not None:
                    def close_reranker_owner():
                        model_lock = getattr(app.state.manager, "_model_lock", None)
                        if model_lock is None:
                            reranker.close()
                        else:
                            with model_lock:
                                reranker.close()

                    await run_in_threadpool(close_reranker_owner)
                if close is not None:
                    await run_in_threadpool(close)
            finally:
                if client is not None:
                    await run_in_threadpool(client.close)
                bindings = getattr(app.state, "model_bindings", None)
                if bindings is not None:
                    await run_in_threadpool(bindings.close)
                call_logs = getattr(app.state, "call_logs", None)
                if call_logs is not None:
                    await run_in_threadpool(call_logs.close)

    app = Starlette(routes=_routes(), lifespan=lifespan)
    from wenjin.management_http.call_logs import CallLogMiddleware, CallLogStore

    call_logs = CallLogStore(Path(root).resolve())
    app.state.call_logs = call_logs
    app.add_middleware(CallLogMiddleware, store=call_logs)

    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _ADMIN_CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=_security_headers)

    app.state.root = Path(root)
    if mcp_control_service is None:
        from wenjin.mcp_control.management import MCPControlService

        mcp_control_service = MCPControlService(Path(root))
    app.state.mcp_control_service = mcp_control_service
    app.state.admin_token = admin_token
    app.state.identity_store = None
    app.state.evaluation_service = None
    app.state.evaluation_job_store = None
    app.state.evaluation_runtime_factory = None
    app.state.settings = settings
    from wenjin.retrieval.search_policy import SearchPolicy

    app.state.search_policy = SearchPolicy(reranker_enabled=False)
    app.state.shared_reranker = None
    app.state.model_bindings = None
    app.state.model_provider_identity = None
    app.state.session_absolute_seconds = 12 * 60 * 60
    app.state.login_attempts = {}
    app.state.max_upload_bytes = max_upload_bytes
    app.state.manager = manager
    app.state.model_bindings = None
    app.state.model_provider_identity = None
    app.state.job_lock = asyncio.Lock()
    app.state.worker_semaphore = asyncio.Semaphore(1)
    app.state.running_jobs = {}
    app.state.release_job_store = None
    app.state.running_release_jobs = {}
    return app
