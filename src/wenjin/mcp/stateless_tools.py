"""Protocol adapter for the isolated 2026-07-28 MCP entry point.

The retrieval handlers stay in :mod:`wenjin.mcp.tools`; this facade only
translates transport-facing result labels, tool error semantics, and download
routes for the new protocol service.
"""

from __future__ import annotations

import inspect
import json
from functools import wraps
from typing import Any, Callable

from mcp.types import CallToolResult, TextContent

from wenjin.mcp.runtime import EXPECTED_PROTOCOL_VERSION as LEGACY_PROTOCOL_VERSION
from wenjin.serving import DOWNLOAD_PREFIX

STATELESS_PROTOCOL_VERSION = "2026-07-28"
STATELESS_DOWNLOAD_PREFIX = "/mcp-stateless" + DOWNLOAD_PREFIX


def _translate_value(value: Any) -> Any:
    """Translate only public response metadata and download paths."""
    if isinstance(value, dict):
        translated = {key: _translate_value(item) for key, item in value.items()}
        if translated.get("mcp_protocol") == LEGACY_PROTOCOL_VERSION:
            translated["mcp_protocol"] = STATELESS_PROTOCOL_VERSION
        path = translated.get("download_path")
        if isinstance(path, str) and path.startswith(DOWNLOAD_PREFIX + "/"):
            translated["download_path"] = STATELESS_DOWNLOAD_PREFIX + path[len(DOWNLOAD_PREFIX):]
        return translated
    if isinstance(value, list):
        return [_translate_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_translate_value(item) for item in value)
    return value


def _adapt_tool_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result

    payload = _translate_value(result)
    has_business_error = bool(payload.get("error"))
    return CallToolResult(
        content=[TextContent(text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))],
        structured_content=payload,
        is_error=has_business_error,
    )


def _adapt_handler(handler: Callable[..., Any]) -> Callable[..., Any]:
    signature = inspect.signature(handler)
    ctx_index = next(
        (index for index, name in enumerate(signature.parameters)
         if name == "ctx" and signature.parameters[name].kind in {
             inspect.Parameter.POSITIONAL_ONLY,
             inspect.Parameter.POSITIONAL_OR_KEYWORD,
         }),
        None,
    )

    def call_without_protocol_session(args: tuple[Any, ...], kwargs: dict[str, Any]):
        positional = list(args)
        named = dict(kwargs)
        if "ctx" in named and named["ctx"] is not None:
            named["ctx"] = StatelessContextProxy(named["ctx"])
        elif ctx_index is not None and ctx_index < len(positional) and positional[ctx_index] is not None:
            positional[ctx_index] = StatelessContextProxy(positional[ctx_index])
        return tuple(positional), named

    if inspect.iscoroutinefunction(handler):

        @wraps(handler)
        async def async_handler(*args: Any, **kwargs: Any) -> Any:
            call_args, call_kwargs = call_without_protocol_session(args, kwargs)
            return _adapt_tool_result(await handler(*call_args, **call_kwargs))

        return async_handler

    @wraps(handler)
    def sync_handler(*args: Any, **kwargs: Any) -> Any:
        call_args, call_kwargs = call_without_protocol_session(args, kwargs)
        return _adapt_tool_result(handler(*call_args, **call_kwargs))

    return sync_handler


class StatelessContextProxy:
    """Keep FastMCP context helpers while hiding its synthetic session UUID."""

    def __init__(self, context: Any) -> None:
        self._context = context

    @property
    def session_id(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)


class StatelessRegistrationFacade:
    """Expose FastMCP registration while adapting the existing business tools."""

    def __init__(self, server: Any) -> None:
        self._server = server

    def __getattr__(self, name: str) -> Any:
        return getattr(self._server, name)

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return self._server.tool(_adapt_handler(args[0]))
        decorator = self._server.tool(*args, **kwargs)

        def register(handler: Callable[..., Any]) -> Any:
            return decorator(_adapt_handler(handler))

        return register

    def custom_route(self, path: str, *args: Any, **kwargs: Any) -> Any:
        if path == DOWNLOAD_PREFIX or path.startswith(DOWNLOAD_PREFIX + "/"):
            path = "/mcp-stateless" + path
        return self._server.custom_route(path, *args, **kwargs)
