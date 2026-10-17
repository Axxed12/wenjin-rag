"""共享资料库与受管文件检索路由。"""

from __future__ import annotations

import re

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from wenjin.management_http.http_common import _jsonable, _manager, _set_retrieval_log_target


def _legacy_service(request: Request):
    service = getattr(request.app.state, "legacy_search", None)
    if service is None:
        manager = request.app.state.manager
        client = getattr(request.app.state, "qdrant_client", None) or getattr(
            manager, "qdrant_client", None
        )
        embedder = getattr(manager, "embedder", None)
        if client is None or embedder is None:
            raise HTTPException(503, "共享资料库检索服务尚未就绪")
        from wenjin.retrieval.legacy_catalog import LegacySearchService

        service = LegacySearchService(
            request.app.state.root,
            client,
            embedder,
            search_lock=getattr(manager, "_model_lock", None),
            expected_embedding_model=getattr(manager, "embedding_model", None),
            reranker=getattr(request.app.state, "shared_reranker", None),
            search_policy=request.app.state.search_policy,
        )
        request.app.state.legacy_search = service
    return service


async def _admin_libraries(request: Request):
    manager = await _manager(request)
    files = await run_in_threadpool(manager.list_files)
    catalog = await run_in_threadpool(_legacy_service(request).catalog, files)
    registered = await run_in_threadpool(manager.list_libraries)
    present = {item.get("library_id") for item in catalog.get("libraries", [])}
    for library in registered:
        if library["library_id"] not in present:
            catalog.setdefault("libraries", []).append({
                "library_id": library["library_id"],
                "library": library["display_name"],
                "sources": [],
                "source_count": 0,
                "managed_library": True,
            })
    return JSONResponse(catalog)


async def _request_json_object(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "请求体必须是JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求体必须是JSON对象")
    return payload


def _validate_admin_search(payload: dict) -> tuple[int, int]:
    query = payload.get("query")
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000:
        raise HTTPException(422, "query长度必须在1到2000字符之间")
    top_k = payload.get("top_k", 5)
    context_char_budget = payload.get("context_char_budget", 8000)
    if (
        not isinstance(top_k, int)
        or isinstance(top_k, bool)
        or not 1 <= top_k <= 20
        or not isinstance(context_char_budget, int)
        or isinstance(context_char_budget, bool)
        or not 500 <= context_char_budget <= 20000
    ):
        raise HTTPException(422, "top_k必须为1到20，context_char_budget必须为500到20000")
    return top_k, context_char_budget


def _managed_search_options(request: Request) -> dict:
    """Managed查询使用同一请求服务已有的重排器和检索策略。"""
    policy = getattr(request.app.state, "search_policy", None)
    reranker = getattr(request.app.state, "shared_reranker", None)
    if policy is not None and not policy.reranker_enabled:
        reranker = None
    return {"reranker": reranker, "search_policy": policy}


async def _admin_search(request: Request):
    manager = await _manager(request)
    payload = await _request_json_object(request)
    top_k, context_char_budget = _validate_admin_search(payload)
    library_id = payload.get("library_id")
    source_type = payload.get("source_type")
    version_id = payload.get("version_id")
    query = payload["query"]
    if not isinstance(library_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", library_id):
        raise HTTPException(422, "library_id格式无效")
    if source_type not in {"shared", "managed"}:
        raise HTTPException(422, "source_type必须是shared或managed")
    if not isinstance(version_id, str) or not re.fullmatch(r"[0-9a-f]{32,64}", version_id):
        raise HTTPException(422, "version_id必须是当前清单中的完整版本身份")
    document_id = payload.get("document_id")
    if document_id is not None and not isinstance(document_id, str):
        raise HTTPException(422, "document_id必须是字符串")

    if source_type == "managed":
        file_id = payload.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            raise HTTPException(422, "managed检索必须提供file_id")
        file_record = await run_in_threadpool(manager.get_file, file_id)
        if file_record.library_id != library_id:
            raise HTTPException(409, "受管文件归属已变化，请刷新资料库清单后重试")
        if document_id is not None and document_id != file_id:
            raise HTTPException(409, "受管文件只允许检索自身文档，请刷新清单后重试")
        _set_retrieval_log_target(request, target_type="file", value=file_id)
        result = await run_in_threadpool(
            manager.search,
            file_id,
            query,
            version_id=version_id,
            expected_library_id=library_id,
            top_k=top_k,
            context_char_budget=context_char_budget,
            **_managed_search_options(request),
        )
        result["catalog_identity"] = {
            "library_id": library_id,
            "source_type": "managed",
            "corpus_id": None,
            "file_id": file_id,
            "version_id": version_id,
            "index_target_id": None,
        }
        return JSONResponse(_jsonable(result))

    corpus_id = payload.get("corpus_id")
    target_id = payload.get("index_target_id")
    if not isinstance(corpus_id, str) or not isinstance(target_id, str):
        raise HTTPException(422, "shared检索必须提供corpus_id和index_target_id")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", corpus_id) or not re.fullmatch(
        r"[A-Za-z0-9._-]{1,128}", target_id
    ):
        raise HTTPException(422, "corpus_id或index_target_id格式无效")
    legacy_service = _legacy_service(request)
    await run_in_threadpool(legacy_service.validate_selection, library_id, corpus_id, target_id)
    _set_retrieval_log_target(request, target_type="index", value=target_id)
    result = await run_in_threadpool(
        legacy_service.search,
        library_id=library_id,
        corpus_id=corpus_id,
        version_id=version_id,
        query=query,
        index_target_id=target_id,
        document_id=document_id,
        top_k=top_k,
        context_char_budget=context_char_budget,
    )
    return JSONResponse(_jsonable(result))


async def _search(request: Request):
    manager = await _manager(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "请求体必须是JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求体必须是JSON对象")
    file_id, query = payload.get("file_id"), payload.get("query")
    if not isinstance(file_id, str) or not isinstance(query, str):
        raise HTTPException(422, "file_id和query必须是字符串")
    version_id = payload.get("version_id")
    if version_id is not None and not isinstance(version_id, str):
        raise HTTPException(422, "version_id必须是字符串")
    if file_id.startswith("legacy:"):
        raise HTTPException(409, "legacy文件尚未adopt到managed索引，当前入口不能检索它")
    if len(query) > 2000:
        raise HTTPException(422, "query长度不能超过2000字符")
    version = payload.get("version")
    if version is not None and not isinstance(version, str):
        raise HTTPException(422, "version必须是字符串")
    top_k = payload.get("top_k", 5)
    context_char_budget = payload.get("context_char_budget", 8000)
    if (
        not isinstance(top_k, int)
        or isinstance(top_k, bool)
        or not isinstance(context_char_budget, int)
        or isinstance(context_char_budget, bool)
    ):
        raise HTTPException(422, "top_k和context_char_budget必须是整数")
    _set_retrieval_log_target(request, target_type="file", value=file_id)
    # manager.search在同一把active锁内先取snapshot，再校验版本和预算；
    # 不用“先list再search”，避免replace刚好发生在两个请求之间。
    result = await run_in_threadpool(
        manager.search,
        file_id,
        query,
        version=version,
        version_id=version_id,
        expected_library_id=getattr(request.state, "managed_authorized_library_id", None),
        top_k=top_k,
        context_char_budget=context_char_budget,
        **_managed_search_options(request),
    )
    return JSONResponse(_jsonable(result))
