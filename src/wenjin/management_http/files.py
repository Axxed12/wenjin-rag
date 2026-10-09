"""受管文件的 HTTP 路由处理。"""

from __future__ import annotations

import re
from functools import partial
from pathlib import Path
from urllib.parse import quote, unquote
from uuid import uuid4

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from wenjin.management_http.http_common import _jsonable, _manager
from wenjin.management_http.jobs import _schedule_job
from wenjin.management_http.search import _legacy_service


def _safe_name(value: str | None) -> str:
    if not value:
        raise HTTPException(400, "缺少 X-File-Name")
    # CLI 对非 ASCII 文件名使用百分号编码；API 也接受未编码的 UTF-8 可见字符。
    name = unquote(value, errors="strict").strip()
    if (
        not name
        or name in {".", ".."}
        or any(char in name for char in ("/", "\\", ":", "<", ">", '"', "|", "?", "*", "\x00"))
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or len(name) > 240
        or name.endswith((".", " "))
    ):
        raise HTTPException(400, "文件名必须是单个安全文件名，长度不超过240字符")
    return name


def _upload_name(request: Request) -> str:
    raw = next(
        (value for key, value in request.scope.get("headers", []) if key.lower() == b"x-file-name"),
        None,
    )
    if raw is None:
        return _safe_name(None)
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "X-File-Name必须使用UTF-8") from exc
    return _safe_name(value)


async def _write_upload(request: Request, manager, display_name: str) -> Path:
    limit = request.app.state.max_upload_bytes
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > limit:
                raise HTTPException(413, f"上传超过{limit}字节上限")
        except ValueError as exc:
            raise HTTPException(400, "Content-Length无效") from exc
    upload_dir = Path(manager.base) / ".incoming"
    upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(display_name).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".bin"
    target = upload_dir / f"{uuid4().hex}{suffix}"
    total = 0
    try:
        with target.open("xb") as stream:
            async for block in request.stream():
                total += len(block)
                if total > limit:
                    raise HTTPException(413, f"上传超过{limit}字节上限")
                stream.write(block)
        if total == 0:
            raise HTTPException(400, "上传内容不能为空")
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


async def _file_list(request: Request):
    manager = await _manager(request)
    records = await run_in_threadpool(manager.list_files)
    return JSONResponse({"files": [_jsonable(item) for item in records]})


async def _managed_library_list(request: Request):
    manager = await _manager(request)
    return JSONResponse({"libraries": await run_in_threadpool(manager.list_libraries)})


async def _managed_library_create(request: Request):
    manager = await _manager(request)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "请求体必须是JSON对象") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求体必须是JSON对象")
    display_name = payload.get("display_name")
    library_id = payload.get("library_id")
    if not isinstance(display_name, str):
        raise HTTPException(422, "请填写资料库名称")
    if library_id is not None and not isinstance(library_id, str):
        raise HTTPException(422, "资料库编号必须是文本")
    # 共享库与受管库使用同一稳定ID空间，创建时明确拒绝冲突。
    from wenjin.retrieval.router import normalize_library

    if library_id:
        normalized_id = normalize_library(library_id)
        if normalized_id != library_id:
            raise HTTPException(422, "资料库编号只能使用小写字母、数字和连字符")
    catalog = await run_in_threadpool(
        _legacy_service(request).catalog, await run_in_threadpool(manager.list_files)
    )
    if not library_id:
        library_id = f"managed-{uuid4().hex[:12]}"
    if any(
        item.get("library_id") == library_id
        for item in catalog.get("libraries", [])
    ):
        raise HTTPException(409, "资料库编号已被现有共享资料库使用")
    if any(
        str(item.get("library") or "").casefold() == display_name.strip().casefold()
        for item in catalog.get("libraries", [])
    ):
        raise HTTPException(409, "资料库名称已被现有共享资料库使用")
    try:
        library = await run_in_threadpool(
            manager.create_library,
            library_id,
            display_name,
            created_by=getattr(getattr(request.state, "principal", None), "user_id", None),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return JSONResponse({"library": library}, status_code=201)


async def _file_status(request: Request):
    manager = await _manager(request)
    file_id = request.path_params["file_id"]
    if file_id.startswith("legacy:"):
        records = await run_in_threadpool(manager.list_files)
        record = next((item for item in records if item.file_id == file_id), None)
        if record is None:
            raise KeyError(file_id)
        return JSONResponse(_jsonable(record))
    record = await run_in_threadpool(manager.get_file, file_id)
    return JSONResponse(_jsonable(record))


async def _file_resources(request: Request):
    manager = await _manager(request)
    file_id = request.path_params["file_id"]
    if file_id.startswith("legacy:"):
        raise HTTPException(409, "legacy资源归共享索引管理，不属于文件管理器的owned范围")
    resources = await run_in_threadpool(manager.inspect_resources, file_id)
    return JSONResponse(_jsonable(resources))


async def _submit_upload(request: Request, *, replace_file_id: str | None = None):
    manager = await _manager(request)
    original_filename = _upload_name(request)
    raw_display = request.headers.get("x-display-name")
    try:
        display_name = _safe_name(unquote(raw_display, errors="strict")) if raw_display else original_filename
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, "显示名称格式无效") from exc
    library_id = request.query_params.get("library_id")
    library_kwargs = {}
    if library_id is not None:
        registered = await run_in_threadpool(manager.list_libraries)
        library = next((item for item in registered if item["library_id"] == library_id), None)
        if library is not None:
            library_kwargs = {"library_id": library_id, "library_name": library["display_name"]}
        else:
            catalog = await run_in_threadpool(
                _legacy_service(request).catalog,
                await run_in_threadpool(manager.list_files),
            )
            library = next(
                (item for item in catalog["libraries"] if item["library_id"] == library_id), None
            )
        if library is None:
            raise HTTPException(422, "library_id不是当前资料库清单中的逻辑库")
        if not library_kwargs:
            library_kwargs = {"library_id": library_id, "library_name": library["library"]}
    temp_path = await _write_upload(request, manager, original_filename)
    try:
        job = await run_in_threadpool(
            manager.submit_import,
            temp_path,
            display_name=display_name,
            original_filename=original_filename,
            replace_file_id=replace_file_id,
            **library_kwargs,
        )
    finally:
        # submit_import在返回前已将上传复制进持久staging目录；HTTP临时副本不依赖重试。
        temp_path.unlink(missing_ok=True)
    await _schedule_job(request.app, job.job_id)
    return JSONResponse(_jsonable(job), status_code=202)


async def _file_upload(request: Request):
    return await _submit_upload(request)


async def _file_replace(request: Request):
    if request.path_params["file_id"].startswith("legacy:"):
        raise HTTPException(409, "legacy文件仍由共享索引管理；请先adopt。替换只作用于managed副本")
    return await _submit_upload(request, replace_file_id=request.path_params["file_id"])


async def _file_adopt(request: Request):
    manager = await _manager(request)
    job = await run_in_threadpool(manager.submit_adopt, request.path_params["file_id"])
    await _schedule_job(request.app, job.job_id)
    return JSONResponse(_jsonable(job), status_code=202)


async def _file_delete(request: Request):
    if request.path_params["file_id"].startswith("legacy:"):
        raise HTTPException(409, "legacy文件仍由共享索引管理；请先adopt。此入口不能删除原legacy文件")
    manager = await _manager(request)
    job = await run_in_threadpool(manager.submit_delete, request.path_params["file_id"])
    await _schedule_job(request.app, job.job_id)
    return JSONResponse(_jsonable(job), status_code=202)


async def _file_download(request: Request):
    manager = await _manager(request)
    if request.path_params["file_id"].startswith("legacy:"):
        raise HTTPException(409, "legacy原文仍由共享资料库管理；请先adopt后通过managed副本下载")
    version_id = request.query_params.get("version_id")
    download = partial(
        manager.get_original,
        request.path_params["file_id"],
        request.query_params.get("path"),
    )
    if version_id is not None:
        download = partial(download, expected_version_id=version_id)
    expected_library_id = getattr(request.state, "managed_authorized_library_id", None)
    if expected_library_id is not None:
        download = partial(download, expected_library_id=expected_library_id)
    data = await run_in_threadpool(download)
    filename = request.query_params.get("name") or request.path_params["file_id"]
    filename = _safe_name(filename)
    media_type = "application/octet-stream"
    return Response(
        data,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}"},
    )
