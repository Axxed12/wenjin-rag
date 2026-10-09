"""Administrator-only evaluation task API routes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from wenjin.evaluation_tasks.service import EvaluationJob
from wenjin.identity.policy import AuthenticationFailed, Conflict, Forbidden, NotFound


def _service(request: Request):
    service = getattr(request.app.state, "evaluation_service", None)
    if service is None:
        raise HTTPException(503, "管理员评测任务服务尚未就绪")
    return service


def _principal(request: Request):
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(401, "需要登录")
    return principal


def _safe_errors(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Keep evaluation preflight/model errors from exposing paths or provider input."""
    async def guarded(request: Request):
        try:
            return await handler(request)
        except (HTTPException, AuthenticationFailed, Conflict, Forbidden, NotFound):
            raise
        except Exception:  # noqa: BLE001 - runtime exception text can contain local paths/secrets
            return JSONResponse(
                {
                    "error": "evaluation_unavailable",
                    "detail": "评测当前不可用，请检查固定题集、资料版本和本机运行时配置",
                },
                status_code=503,
            )
    return guarded


def _public_binding(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    allowed = {
        "dataset_id", "dataset_sha256", "route_id", "route_sha256", "profiles",
        "target_ids", "release_ids", "target_count", "dataset_case_count",
        "code_sha256", "profile_sha256", "embedding_model", "reranker_model",
        "embedding_model_sha256", "reranker_model_sha256", "assets_sha256",
        "run_identity_sha256", "created_at", "experiment_id", "config_sha256",
        "snapshot_revision", "embedding_model_id", "reranker_model_id",
        "rewriter_model_id", "embedding_model_sha256", "reranker_model_sha256",
        "case_ids", "resource_ids", "config",
    }
    return {key: item for key, item in value.items() if key in allowed}


def _job_json(job: EvaluationJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "experiment_id": job.experiment_id,
        "title": job.title,
        "owner_user_id": job.owner_user_id,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "stage": job.stage,
        "completed_cases": job.completed_cases,
        "total_cases": job.total_cases,
        "cancel_requested": job.cancel_requested,
        "input_snapshot": _public_binding(job.input_snapshot),
        "run_identity": _public_binding(job.run_identity),
        # 逐case证据/summary仅通过需当前资料库授权的/result接口读取。
        "result_summary": None,
        "error_code": job.error_code,
        "revision": job.revision,
    }


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as error:
        raise HTTPException(400, "请求体必须是JSON") from error
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求体必须是JSON对象")
    return payload


async def _missing_library_access(
    service: Any, actor: Any, spec: Any, *, active_resource_ids: set[str] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    missing = await run_in_threadpool(service.missing_libraries, actor, spec)
    if active_resource_ids is None:
        resources = await run_in_threadpool(service.identity_store.list_resources, actor)
        active_resource_ids = {
            item["resource_id"] for item in resources
            if item.get("resource_kind") == "library" and item.get("active")
        }
    return missing, tuple(item for item in missing if item in active_resource_ids)


async def _templates(request: Request):
    service = _service(request)
    actor = _principal(request)
    await run_in_threadpool(service._require_admin, actor)
    resources = await run_in_threadpool(service.identity_store.list_resources, actor)
    active_resource_ids = {
        item["resource_id"] for item in resources
        if item.get("resource_kind") == "library" and item.get("active")
    }
    templates = []
    for spec in service.specs.values():
        missing, discoverable = await _missing_library_access(
            service, actor, spec, active_resource_ids=active_resource_ids,
        )
        templates.append({
            "experiment_id": spec.experiment_id,
            "title": spec.title,
            "dataset_id": spec.dataset_id,
            "profiles": list(spec.profiles),
            "max_cases": spec.max_cases,
            "can_submit": not missing,
            "missing_library_count": len(missing),
            "discoverable_missing_libraries": list(discoverable),
        })
    return JSONResponse({"templates": templates})


async def _submit(request: Request):
    payload = await _json_object(request)
    if set(payload) != {"experiment_id"} or not isinstance(payload.get("experiment_id"), str):
        raise HTTPException(422, "仅允许提交白名单experiment_id")
    service, actor = _service(request), _principal(request)
    await run_in_threadpool(service._require_admin, actor)
    spec = service.specs.get(payload["experiment_id"])
    if spec is None:
        raise HTTPException(404, "评测模板不存在或未开放")
    missing, discoverable = await _missing_library_access(service, actor, spec)
    if missing:
        return JSONResponse({
            "error": "evaluation_library_access_required",
            "reason_code": "library_read_grant_missing",
            "detail": f"当前账号缺少此模板所需资料库的读取授权（{len(missing)}项）。请到“身份与授权”逐库授权后重试。",
            "missing_library_count": len(missing),
            "discoverable_missing_libraries": list(discoverable),
        }, status_code=403)
    try:
        job = await service.submit(actor, payload["experiment_id"])
    except Forbidden:
        # Grants may be revoked after preflight. Re-evaluate without weakening the submit gate.
        await run_in_threadpool(service._require_admin, actor)
        missing, discoverable = await _missing_library_access(service, actor, spec)
        if missing:
            return JSONResponse({
                "error": "evaluation_library_access_required",
                "reason_code": "library_read_grant_missing",
                "detail": "评测提交期间资料库读取授权发生变化，请到“身份与授权”检查授权后重试。",
                "missing_library_count": len(missing),
                "discoverable_missing_libraries": list(discoverable),
            }, status_code=403)
        return JSONResponse({
            "error": "evaluation_forbidden",
            "reason_code": "evaluation_access_denied",
            "detail": "当前账号无权提交此评测，请确认管理员身份和账号状态。",
        }, status_code=403)
    return JSONResponse(_job_json(job), status_code=202)


async def _list(request: Request):
    limit, offset = request.query_params.get("limit", "50"), request.query_params.get("offset", "0")
    try:
        page_size, page_offset = int(limit), int(offset)
    except ValueError as error:
        raise HTTPException(422, "limit/offset必须为整数") from error
    jobs, total = await _service(request).list_jobs(
        _principal(request), limit=page_size, offset=page_offset,
    )
    return JSONResponse({"jobs": [_job_json(job) for job in jobs], "total": total,
                         "limit": page_size, "offset": page_offset})


async def _status(request: Request):
    job = await _service(request).get_job(_principal(request), request.path_params["job_id"])
    return JSONResponse(_job_json(job))


async def _cancel(request: Request):
    job = await _service(request).cancel(_principal(request), request.path_params["job_id"])
    return JSONResponse(_job_json(job), status_code=202)


async def _resume(request: Request):
    job = await _service(request).resume(_principal(request), request.path_params["job_id"])
    return JSONResponse(_job_json(job), status_code=202)


async def _result(request: Request):
    case_id = request.query_params.get("case_id")
    filename = request.query_params.get("file", "summary.json")
    path: Path = await _service(request).result_path(
        _principal(request), request.path_params["job_id"], filename, case_id=case_id,
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(409, "评测结果文件损坏") from error
    return JSONResponse(payload)


async def _audit(request: Request):
    try:
        limit = int(request.query_params.get("limit", "100"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError as error:
        raise HTTPException(422, "limit/offset必须为整数") from error
    rows, total = await _service(request).list_audit(
        _principal(request), limit=limit, offset=offset,
    )
    return JSONResponse({"events": rows, "total": total, "limit": limit, "offset": offset})


def evaluation_routes(wrap: Callable[..., Any]) -> list[Route]:
    """Install routes through management app's standard identity/error wrapper."""
    def safe(handler: Callable[..., Any]):
        return wrap(_safe_errors(handler))

    return [
        Route("/api/admin/evaluations/templates", safe(_templates), methods=["GET"]),
        Route("/api/admin/evaluations/audit", safe(_audit), methods=["GET"]),
        Route("/api/admin/evaluations", safe(_list), methods=["GET"]),
        Route("/api/admin/evaluations", safe(_submit), methods=["POST"]),
        Route("/api/admin/evaluations/{job_id:str}", safe(_status), methods=["GET"]),
        Route("/api/admin/evaluations/{job_id:str}/cancel", safe(_cancel), methods=["POST"]),
        Route("/api/admin/evaluations/{job_id:str}/resume", safe(_resume), methods=["POST"]),
        Route("/api/admin/evaluations/{job_id:str}/result", safe(_result), methods=["GET"]),
    ]
