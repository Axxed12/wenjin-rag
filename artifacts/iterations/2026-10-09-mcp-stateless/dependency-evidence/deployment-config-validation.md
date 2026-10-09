# Stateless deployment config syntax checks

Date: 2026-10-09. These are static configuration checks only; they do not start any existing stack or read secret file contents.

## Caddyfile

Command:

```powershell
$file = (Resolve-Path deploy/Caddyfile.stateless).Path
docker run --rm -v "${file}:/etc/caddy/Caddyfile:ro" -e PUBLIC_DOMAIN=rag.example.invalid caddy:2.10-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Output ended with `Valid configuration`. Caddy emitted warnings that explicitly forwarded `X-Forwarded-For`, `X-Forwarded-Proto`, and `X-Forwarded-Host` headers duplicate reverse_proxy defaults; they were left explicit to preserve the deployment's documented proxy header contract. No backend service was contacted.

## Compose merge

Command: `docker compose --project-directory . -f deploy/compose.base.yaml -f deploy/compose.cloud.yaml -f deploy/compose.stateless.yaml config --format json`, with only placeholder nonsecret process environment values for required domain, host, CA path and secret path interpolation. The paths pointed at the existing nonsecret `deploy/.env.example`; compose config does not read secret contents.

Initial observed summary (from the earlier, superseded read-only identity mount):

```json
{"services":"cloud-edge,management,mcp,mcp-stateless,qdrant","statelessImage":"wenjin-mcp-stateless:2026-10-09","statelessReadOnly":true,"identityMountReadOnly":true,"caddyfile":"C:\\Users\\Onebiter\\.codex\\worktrees\\2a22\\rag-project\\deploy\\Caddyfile.stateless"}
```

This initial render predates the WAL fix. Its read-only identity mount is superseded by the final render below; preserve it as the historical configuration that produced the WAL open failure.

## Final overlay after WAL/query-only adapter

Command (static only):

```powershell
docker compose --project-directory . -f deploy/compose.base.yaml -f deploy/compose.cloud.yaml -f deploy/compose.stateless.yaml config --format json
```

The command used only placeholder nonsecret process environment values for required interpolation. It did not start or modify a stack. Parsed final summary:

```json
{
  "services": ["cloud-edge", "management", "mcp", "mcp-stateless", "qdrant"],
  "statelessImage": "wenjin-mcp-stateless:2026-10-09",
  "containerReadOnly": true,
  "identityMountReadOnly": false,
  "identityMountSource": "wenjin-identity",
  "statelessSecrets": ["model_api_token"],
  "fileAdminSecretAttached": false,
  "cloudCaddyfile": "deploy/Caddyfile.stateless",
  "statelessProxyIp": "172.30.250.5"
}
```

The base Compose file still defines `file_admin_token` for the management service. The stateless MCP service has no attachment or alias for that secret. Identity volume filesystem write access is required for SQLite WAL/SHM sidecars; `StatelessIdentityStore` enforces SQLite `query_only` and refuses initialization. This config render does not prove container-level permissions, WAL/revocation behavior, startup, TLS routing or SDK calls; the test agent owns those checks.
