# Modern MCP network client

This isolated harness uses the official `@modelcontextprotocol/client` package pinned to `2.3.1`. It runs a Streamable HTTP client against a loopback endpoint only.

Set these PowerShell environment variables before running it:

- `MCP_STATELESS_URL`: local modern MCP URL, for example `http://127.0.0.1:8123/mcp-stateless`.
- `MCP_STATELESS_TOKEN`: disposable credential issued by the synthetic test identity store.
- `MCP_STATELESS_TOOL`: the registered synthetic acceptance tool.
- `MCP_STATELESS_ARGUMENTS_JSON`: JSON object with synthetic file/release/query arguments.
- `MCP_STATELESS_SESSION_ID`: optional legacy header value for the explicit compatibility probe; unset for normal modern requests.
- `NODE_EXTRA_CA_CERTS`: optional temporary CA path for local HTTPS proxy acceptance. Set it before starting Node.
- `MCP_MODERN_PYTHON`: optional path to the isolated Python MCP SDK 2.x environment used by the era-isolation pytest. If unset, the test checks the task's temporary probe environment and skips when it is absent.
- `MCP_LEGACY_PYTHON`: path to a read-only Python MCP SDK 1.x environment for era-isolation pytest; the default is the original project's `.venv` on `E:` and the test skips if unavailable.

Run `npm ci` in this directory, then `npm run accept`. The SDK client pins protocol version `2026-07-28`, so it rejects a legacy-only server instead of falling back. The output includes protocol metadata, the list/call result, and a redacted record of the outgoing method/header/meta fields. It never prints the bearer token or tool arguments. Use only synthetic fixture inputs and short-lived credentials.

The final positive deployment acceptance is opt-in and uses the official image launcher, temporary Qdrant/index/identity/model fixtures, the pinned TypeScript SDK, and Caddy internal TLS. Run it alone in PowerShell:

```powershell
$env:MCP_STATELESS_CONTAINER_ACCEPTANCE = "1"
python -m pytest -q tests/test_mcp_stateless_launcher_runtime.py
Remove-Item Env:MCP_STATELESS_CONTAINER_ACCEPTANCE
```

It creates uniquely named containers/network and loopback-published ports. It loads `deploy/Caddyfile.stateless` and substitutes only the local domain, internal TLS directive, and synthetic legacy upstream port. The SDK searches and downloads through `/mcp-stateless`; the test also sends `/mcp` and `/files/*` through a synthetic legacy upstream and the management fallback through the real `create_app` account/session/CSRF routes. Those legacy path probes do not perform a legacy MCP handshake over TLS. The model, index, corpus, and identities are synthetic; no real model call or account is used.

`tests/test_mcp_stateless_container_tls.py` preserves the earlier read-only identity-volume/WAL failure probe. It is intentionally skipped by default; set `MCP_STATELESS_READONLY_FAILURE_PROBE=1` only when reproducing that historical negative result. It is not part of the positive deployment command and does not replace the final read-write-sidecar `StatelessIdentityStore` acceptance. The test uses the image selected by `MCP_STATELESS_IMAGE` or the default tag, so use a separately retained historical image only if reproducing its exact original build.
