"""并发受限的源文件取件、完整性校验与下载审计。"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from functools import partial

from fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.responses import Response as StarletteResponse

from wenjin.access import AccessDenied, AccessRegistry, AccessSnapshotError
from wenjin.audit import SearchAudit
from wenjin.corpus_identity import CorpusIdentityError
from wenjin.identity.mcp_auth import identity_from_scope, managed_bridge_headers
from wenjin.mcp.managed_client import (
    ManagedFileConflict,
    ManagedFileForbidden,
    ManagedFileHTTPClient,
    ManagedFileServiceError,
    ManagedFileUnauthorized,
    require_managed_file_access,
)
from wenjin.mcp.tools import client_identity, policy_unavailable
from wenjin.serving import (
    DOWNLOAD_PREFIX,
    CorpusSources,
    SourceModified,
    SourceNotFound,
    SourceTooLarge,
    SourceUnverified,
)

MAX_CONCURRENT_DOWNLOADS = 2

class SlotHoldingResponse(StarletteResponse):
    """把并发槽位持有到**正文发送结束**的响应包装。

    只包住"读取文件+构造响应"是不够的：函数一返回槽位就被释放，而框架才开始发送正文，
    此时那个已经读进内存的文件缓冲区还被慢客户端占着，新请求却能继续读取——内存上界
    就不成立了。所以槽位必须留到发送真正结束。

    `finally` 保证正常发送完、发送抛异常、客户端断开（发送协程被取消）都会通知槽位，
    不会有请求永久占着槽位。
    """

    def __init__(self, inner: StarletteResponse, on_settled: Callable[[], None]) -> None:
        super().__init__(status_code=inner.status_code)
        self._inner = inner
        self._on_settled = on_settled

    async def __call__(self, scope, receive, send) -> None:
        try:
            await self._inner(scope, receive, send)
        finally:
            self._on_settled()


class DownloadSlot:
    """一次取件对并发槽位的持有权。

    槽位必须**同时**满足两件事才释放：

    1. **后台读取线程已经结束**。请求协程被取消不会停掉执行器里的线程，线程还占着刚读进
       内存的文件缓冲区；如果这时就释放槽位，后续请求会开始新的读取，声明的并发与内存
       上界就不成立（实测取消两次后读取并发峰值达到3，而上限是2）。
    2. **正文已经发完**，或确定不会有正文（读取抛异常/被取消）。

    两个标记由不同来源设置：线程结束由**独立工作任务**的 `finally` 设置（该任务不随请求
    协程取消，否则会在线程还在读时就被标记为已结束——这正是第一版的错误），正文结束由
    响应发送或异常分支设置。最后满足条件的那个负责释放，且只释放一次。
    """

    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._thread_finished = False
        self._body_settled = False
        self._released = False

    def mark_thread_finished(self) -> None:
        self._thread_finished = True
        self._maybe_release()

    def mark_body_sent(self) -> None:
        self._body_settled = True
        self._maybe_release()

    def mark_no_body(self) -> None:
        """确定不会发送正文：读取抛了异常，或请求在被取消的状态下结束。"""
        self._body_settled = True
        self._maybe_release()

    def _maybe_release(self) -> None:
        if self._thread_finished and self._body_settled and not self._released:
            self._released = True
            self._release()

def serve_download(
    *,
    corpus_id: str,
    relative_path: str,
    client_id: str | None,
    release_id: str | None = None,
    current_access: Callable[[], AccessRegistry],
    sources_for: Callable[[str], CorpusSources],
    audit: SearchAudit,
    identity_store=None,
    principal=None,
    library_id: str | None = None,
) -> Response:
    """取件的完整判定，独立成函数以便直接测试和审计。

    四个条件必须同时满足：调用方获准访问该语料、文件登记在manifest中、
    路径位于该语料可信目录内、文件字节符合登记的raw_sha256。
    """
    started = time.perf_counter()

    def finish(outcome: str, status: int, payload: dict, *, bytes_served: int | None = None,
               detail: str | None = None, response: Response | None = None) -> Response:
        audit.record_download(
            corpus_id=corpus_id, relative_path=relative_path, client_id=client_id,
            outcome=outcome, elapsed_seconds=time.perf_counter() - started,
            bytes_served=bytes_served, detail=detail,
        )
        return response if response is not None else JSONResponse(payload, status_code=status)

    if identity_store is not None:
        try:
            if principal is None or not library_id or not identity_store.can_read(
                principal, "library", library_id
            ):
                raise AccessDenied("当前账号没有该资料库的读取授权")
        except AccessDenied as exc:
            return finish("denied", 403, {"error": "forbidden", "detail": str(exc)})
    else:
        try:
            registry = current_access()
        except AccessSnapshotError as exc:
            return finish("error", 503,
                          {"error": "unavailable",
                           "detail": policy_unavailable(exc, tool="download")},
                          detail=str(exc))
        try:
            registry.require(corpus_id, client_id)
        except AccessDenied as exc:
            return finish("denied", 403, {"error": "forbidden", "detail": str(exc)})

    # 缺Hash可能在**两个阶段**暴露：加载manifest时（content_hash_of校验每条登记记录）
    # 和读取文件时。两处都要给出结构化拒绝并留下审计——只捕获后一处的话，
    # manifest阶段的异常会变成500且不留任何记录。
    unverified_detail = "语料的原文校验信息不完整，拒绝返回内容"

    try:
        if release_id:
            sources = sources_for(corpus_id, release_id=release_id)
        else:
            sources = sources_for(corpus_id)
    except SourceNotFound as exc:
        return finish("not_found", 404, {"error": "not_found", "detail": str(exc)})
    except SourceUnverified as exc:
        # 部署问题：给客户端通用说明，具体缺哪条记录只进服务端审计。
        return finish("unverified", 409,
                      {"error": "source_unverified", "detail": unverified_detail},
                      detail=str(exc))
    except (CorpusIdentityError, ValueError):
        # 身份冲突或manifest损坏属于部署错误：拒绝，且不向客户端透露细节。
        return finish("unavailable", 503, {"error": "unavailable",
                                           "detail": "语料配置无效，请联系管理员"})

    try:
        verified = sources.read_verified(relative_path)
    except SourceNotFound as exc:
        return finish("not_found", 404, {"error": "not_found", "detail": str(exc)})
    except SourceModified as exc:
        # 语料被改动过：宁可拒绝，也不能把"和登记Hash不符"的内容当原文送出去。
        return finish("hash_mismatch", 409, {"error": "source_modified", "detail": str(exc)})
    except SourceUnverified as exc:
        # 没有可用Hash就无法证明取回的是被引用的那份原文，只能拒绝。
        return finish("unverified", 409,
                      {"error": "source_unverified", "detail": unverified_detail},
                      detail=str(exc))
    except SourceTooLarge as exc:
        return finish("too_large", 413, {"error": "too_large", "detail": str(exc)})

    return finish(
        "served", 200, {},
        bytes_served=len(verified.content),
        response=StarletteResponse(
            content=verified.content,
            media_type=verified.media_type,
            headers=verified.response_headers(),
        ),
    )

def register_download_route(
    mcp: FastMCP, *, bearer_enabled: bool, audit: SearchAudit,
    current_access: Callable[[], AccessRegistry],
    sources_for: Callable[[str], CorpusSources],
    identity_store=None,
    root=None,
    release_resolver=None,
    managed_bridge=None,
) -> None:
    # 槽位与后台任务集合各属于这个服务实例，绝不跨实例共享。
    download_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    background_tasks: set[asyncio.Task] = set()

    def current_managed_bridge() -> ManagedFileHTTPClient | None:
        return managed_bridge() if callable(managed_bridge) else managed_bridge

    @mcp.custom_route(f"{DOWNLOAD_PREFIX}/managed/{{file_id}}", methods=["GET"])
    async def download_managed_source(request: Request) -> Response:
        file_id = request.path_params["file_id"]
        version_id = request.query_params.get("version_id", "")
        if not re.fullmatch(r"[0-9a-f]{32,64}", version_id):
            return JSONResponse({"error": "invalid_version", "detail": "必须提供完整version_id"}, status_code=422)
        bridge = current_managed_bridge()
        if bridge is None:
            return JSONResponse({"error": "unavailable", "detail": "managed文件服务暂不可用"}, status_code=503)
        authorization = None
        if identity_store is not None:
            try:
                identity_from_scope(request.scope)
                authorization = managed_bridge_headers(request.scope)["Authorization"]
            except Exception:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        else:
            client_id = client_identity(bearer_enabled=bearer_enabled)
            try:
                require_managed_file_access(current_access(), file_id, client_id)
            except (AccessDenied, AccessSnapshotError, ValueError):
                return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            upstream = bridge.download_source(file_id, version_id, authorization=authorization)
        except KeyError:
            return JSONResponse({"error": "not_found"}, status_code=404)
        except ManagedFileUnauthorized:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        except ManagedFileForbidden:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        except ManagedFileConflict:
            return JSONResponse({"error": "stale_version"}, status_code=409)
        except ManagedFileServiceError:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        headers = {
            key: value for key, value in upstream.headers.items()
            if key.lower() in {"content-disposition", "cache-control", "x-content-type-options"}
        }
        return Response(
            content=upstream.content,
            media_type=upstream.headers.get("content-type", "application/octet-stream"),
            headers=headers,
        )

    @mcp.custom_route(f"{DOWNLOAD_PREFIX}/{{corpus_id}}/{{file_path:path}}", methods=["GET"])
    async def download_source(request: Request) -> Response:
        """按语料内相对路径返回原文；Bearer由中间件校验，这里只管授权、登记与字节校验。

        取件要读文件、算Hash，可能几十MB，全是同步阻塞操作。放在事件循环里会卡住
        同一进程的其他连接（实测一次200ms读取会把本该20ms的定时任务推迟到203ms），
        所以丢到有界线程池执行；同时用信号量限制并发，避免多个大文件同时占满内存。
        """
        principal = None
        library_id = None
        if identity_store is not None:
            principal = identity_from_scope(request.scope).principal
            from wenjin.retrieval.router import normalize_library

            try:
                release_id = request.query_params.get("release_id")
                if release_id:
                    from wenjin.index_registry import IndexTarget
                    from wenjin.releases.catalog import ReleaseCatalog
                    from wenjin.releases.resolver import release_catalog_path

                    catalog = ReleaseCatalog(release_catalog_path(root))
                    record = catalog.get_release(release_id)
                    target = IndexTarget.from_dict(record["target"])
                    if target.corpus_identity() != request.path_params["corpus_id"]:
                        raise ValueError("release与corpus不匹配")
                    library_id = normalize_library(target.library)
                else:
                    from wenjin.catalog import find_corpus

                    entry = find_corpus(root, request.path_params["corpus_id"])
                    library_id = normalize_library(entry.library) if entry is not None else None
            except Exception:
                library_id = None
            client_id = principal.user_id
        else:
            client_id = client_identity(bearer_enabled=bearer_enabled)
        # 路径参数在事件循环里取好，线程里不再碰请求对象。
        corpus_id = request.path_params["corpus_id"]
        relative_path = request.path_params["file_path"]
        release_id = request.query_params.get("release_id")

        # 手动acquire而不是async with：槽位要留到读取线程结束且正文发完，见DownloadSlot。
        await download_slots.acquire()
        slot = DownloadSlot(download_slots.release)

        async def run_work() -> StarletteResponse:
            try:
                return await run_in_threadpool(
                    partial(
                        serve_download,
                        corpus_id=corpus_id,
                        relative_path=relative_path,
                        client_id=client_id,
                        release_id=release_id,
                        current_access=current_access,
                        sources_for=sources_for,
                        audit=audit,
                        identity_store=identity_store,
                        principal=principal,
                        library_id=library_id,
                    )
                )
            finally:
                # 只有线程真正结束才会走到这里。run_work跑在独立任务里，
                # 不随请求协程取消——否则会在线程还在读时就把槽位标记为已结束。
                slot.mark_thread_finished()

        # 独立任务 + shield：请求被取消时工作继续跑完，槽位由它的 finally 释放。
        work = asyncio.create_task(run_work())
        background_tasks.add(work)  # 持强引用，避免任务被GC提前回收
        work.add_done_callback(background_tasks.discard)
        try:
            response = await asyncio.shield(work)
        except BaseException:
            # 取消或异常都**不**在这里释放槽位：线程可能还在读。只声明"不会有正文"，
            # 由 mark_thread_finished 与它共同决定何时真正释放。
            slot.mark_no_body()
            raise
        return SlotHoldingResponse(response, slot.mark_body_sent)
