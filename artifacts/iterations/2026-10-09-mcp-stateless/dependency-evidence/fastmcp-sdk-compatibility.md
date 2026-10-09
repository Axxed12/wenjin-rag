# Dependency and framework probe (2026-10-09)

Scope: isolated read-only dependency probe against the worktree source. No service was started; no `.env`, corpus, index, account, or database was opened. The probe virtual environment was created under `%TEMP%\codex-mcp-stateless-probe-20261009`, outside the worktree. It contains packages only for dependency/API inspection and imports the worktree package in editable `--no-deps` mode.

## Worktree dependency facts

Command:

```powershell
Select-String -Path pyproject.toml -Pattern 'fastmcp|requires-python'
Select-String -Path uv.lock -Pattern '^name = "fastmcp"|^version = "3.4.4"|^name = "mcp"|^version = "1.29.0"' -Context 0,0
```

Observed: project requires Python `>=3.12,<3.13`, pins `fastmcp==3.4.4`; the lock has `fastmcp==3.4.4` and official `mcp==1.29.0`.

## FastMCP 4.1.0 + official SDK 2.3.0 resolver

Command:

```powershell
uv pip install --dry-run fastmcp==4.1.0 mcp==2.3.0
```

Output:

```text
Using Python 3.12.3 environment at: C:\Users\Onebiter\AppData\Local\hermes\hermes-agent\venv
Resolved 69 packages in 54ms
Would download 14 packages
Would uninstall 1 package
Would install 35 packages
 - mcp==1.28.0
 + fastmcp==4.1.0
 + fastmcp-slim==4.1.0
 + mcp==2.3.0
 + mcp-types==2.3.0
```

The combined requirements resolve; SDK v2 replaces the v1 SDK and adds exactly matched `mcp-types==2.3.0`.

## FastMCP 3.4.4 + official SDK 2.3.0 resolver

Command:

```powershell
uv pip install --dry-run fastmcp==3.4.4 mcp==2.3.0
```

Output:

```text
Using Python 3.12.3 environment at: C:\Users\Onebiter\AppData\Local\hermes\hermes-agent\venv
  x No solution found when resolving dependencies:
    Because fastmcp-slim[client]==3.4.4 depends on mcp>=1.24.0,<2.0 and
    fastmcp==3.4.4 depends on fastmcp-slim[client]==3.4.4, we can conclude
    that fastmcp==3.4.4 depends on mcp>=1.24.0,<2.0.
    And because you require fastmcp==3.4.4 and mcp==2.3.0, we can conclude
    that your requirements are unsatisfiable.
```

## Isolated FastMCP 4.1.0 + SDK 2.3.0 API probe

Commands (first creates a temporary isolated venv, then installs FastMCP 4.1.0 and SDK 2.3.0):

```powershell
$probeDir = Join-Path $env:TEMP 'codex-mcp-stateless-probe-20261009'
uv venv $probeDir
uv pip install --python (Join-Path $probeDir 'Scripts\python.exe') fastmcp==4.1.0 mcp==2.3.0
```

The isolated install completed with `fastmcp==4.1.0`, `mcp==2.3.0`, `mcp-types==2.3.0`.

To permit import of project MCP modules, the isolated venv then received the runtime packages declared in `pyproject.toml` except its old FastMCP pin, and the worktree package was installed using `uv pip install --no-deps -e .`. No model extras, service, or data path was activated.

Probe command:

```powershell
& (Join-Path $probeDir 'Scripts\python.exe') -c "from wenjin.mcp.tools import register_tools; import inspect; from fastmcp import FastMCP, Context; from fastmcp.server.dependencies import get_http_request, get_http_headers; print('tools_import_ok', callable(register_tools)); print('FastMCP tool signature', inspect.signature(FastMCP.tool)); print('Context.session_id attr', Context.session_id); print('modern HTTP app', hasattr(FastMCP('x'), 'http_app')); print('run signature', inspect.signature(FastMCP.run))"
```

Output:

```text
tools_import_ok True
FastMCP tool signature (self, name_or_fn: 'str | AnyFunction | None' = None, *, name: 'str | None' = None, version: 'str | int | None' = None, title: 'str | None' = None, description: 'str | None' = None, icons: 'list[mcp_types.Icon] | None' = None, tags: 'set[str] | None' = None, output_schema: 'dict[str, Any] | NotSetT | None' = Ellipsis, annotations: 'ToolAnnotations | dict[str, Any] | None' = None, meta: 'dict[str, Any] | None' = None, app: 'AppConfig | dict[str, Any] | bool | None' = None, task: 'bool | TaskConfig | None' = None, timeout: 'float | None' = None, auth: 'AuthCheck | list[AuthCheck] | None' = None, run_in_thread: 'bool' = True) -> 'Callable[[AnyFunction], FunctionTool] | FunctionTool | partial[Callable[[AnyFunction], FunctionTool] | FunctionTool]'
Context.session_id attr <property object at ...>
modern HTTP app True
run signature (self: 'FastMCP', transport: 'Transport | None' = None, show_banner: 'bool | None' = None, **transport_kwargs: 'Any') -> 'None'
```

Separate existence checks returned `get_http_request=True`, `get_http_headers=True`; `FastMCP 4.1.0` exposes `http_app()` and not `streamable_http_app()`. Building `m.http_app(path='/mcp')` returned `fastmcp.server.http.StarletteWithLifespan` with route `('/mcp', [])`.

The `register_tools` call itself was exercised without invoking tools or connecting external services:

```powershell
& (Join-Path $probeDir 'Scripts\python.exe') -c "import asyncio; from pathlib import Path; from fastmcp import FastMCP; from wenjin.mcp.tools import register_tools; m=FastMCP('probe'); register_tools(m,root=Path('.'),raw_root_path=Path('.'),audit=object(),access=None,access_dir=Path('.'),bearer_enabled=False,identity=None,service=None,corpus_router=None,release_resolver=None,managed_bridge=None); tools=asyncio.run(m.list_tools()); print('registered_tool_names', [t.name for t in tools]); print('registered_count',len(tools))"
```

Output:

```text
registered_tool_names ['search_docs', 'list_corpora', 'list_managed_files']
registered_count 3
```

## What this proves and does not prove

- FastMCP 3.4.4 and SDK 2.3.0 cannot be installed together under package constraints.
- FastMCP 4.1.0 and SDK 2.3.0 resolve together and can import this worktree's `register_tools` and register all three existing tools unchanged. The current helper imports (`get_http_request`, `get_http_headers`) and `run_in_thread` parameter remain present.
- This is a synthetic registration/API probe only. It does not prove modern wire conformance, per-request authorization, response/error semantics, client interoperability, model-backed retrieval, managed-file retrieval, version binding, revoke handling, or container/proxy behavior.
- `Context.session_id` was checked only as a property descriptor; the value under an actual modern request was not exercised. Existing `audit_session_id` catches `RuntimeError`, but runtime behavior remains to be tested.
- Existing tool payloads currently write `mcp_protocol="2025-11-25"`; a new protocol adapter needs to correct/omit that field for modern calls without changing legacy behavior.
- Existing FastMCP 3 `server.py` invokes `run(transport="streamable-http", stateless_http=False, ...)` and asserts the v1 SDK protocol constant equals the old version. A separate FastMCP 4 entry point can reuse tool registrations, but needs its own transport/app wiring and tests. This probe did not test protocol rejection/isolation.

## Independent lock and modern protocol API check

The separate `mcp-stateless/pyproject.toml` pins `fastmcp==4.1.0` and `mcp==2.3.0`, leaving the original project dependency files unchanged. Lock generation command:

```powershell
uv lock --project mcp-stateless
```

Observed output: `Resolved 141 packages in 10.18s`; `mcp-stateless/uv.lock` was created. This dependency lock is isolated from the main project lock.

Runtime API probe command:

```powershell
$python = Join-Path $env:TEMP 'codex-mcp-stateless-probe-20261009\Scripts\python.exe'
& $python -c "from mcp.types import LATEST_PROTOCOL_VERSION; from mcp.server.lowlevel.server import MODERN_PROTOCOL_VERSIONS; from fastmcp import FastMCP; print('protocol', LATEST_PROTOCOL_VERSION); print('modern_versions', MODERN_PROTOCOL_VERSIONS); print('http_app', hasattr(FastMCP, 'http_app'))"
```

Output:

```text
protocol 2026-07-28
modern_versions ('2026-07-28',)
http_app True
```

This confirms the selected SDK's current modern version constant/list and the FastMCP 4 ASGI app API. The stateless builder checks these at startup so running it from the legacy environment fails explicitly. The SDK's actual `server/discover` response and transport wire behavior are left for the independent client acceptance; this API check alone does not prove them.

The new entry module guard and lock consistency were then exercised:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& $python -c "from wenjin.mcp.stateless_server import _validate_stateless_runtime; _validate_stateless_runtime(); print('runtime_guard=pass')"
uv lock --check --project mcp-stateless
```

Output:

```text
runtime_guard=pass
Resolved 141 packages in 1ms
```

The lock lists `fastmcp==4.1.0` and `mcp==2.3.0` explicitly. The guard was only run inside the isolated probe venv; the main legacy environment was not changed.
