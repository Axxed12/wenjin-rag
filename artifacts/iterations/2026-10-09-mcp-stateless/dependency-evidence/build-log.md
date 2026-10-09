# Isolated stateless image build

Date: 2026-10-09. Docker builds created/replaced only the new `wenjin-mcp-stateless:2026-10-09` image tag. The final CLI `--help` smoke test ran a short-lived `--rm` container with no listener, credentials, mounts or project data. No existing application container, service, volume, or old image was changed.

Command:

```powershell
docker build --file deploy/Dockerfile.mcp-stateless --tag wenjin-mcp-stateless:2026-10-09 .
```

The last rebuild also added a fail-closed guard: `create_stateless_app` raises when both the IdentityStore and static Bearer credential are absent. A synthetic call to the factory confirmed this refusal. The final rebuild followed the WAL fix: the dedicated `StatelessIdentityStore` sets `PRAGMA query_only=ON` on every connection and refuses initialization. The stateless Compose overlay mounts the identity volume read-write solely so SQLite can maintain WAL/SHM coordination sidecars. This is logical SQL read-only, not a read-only filesystem mount. A synthetic local WAL probe confirmed reads with no pre-existing sidecars, write rejection, unchanged main DB SHA-256 from reader activity, and visibility of an independent writer update on the next connection. The isolated container/TLS/WAL acceptance remains owned by the test agent.

Result: successful BuildKit build using `python:3.12.3-slim-bookworm` and `ghcr.io/astral-sh/uv:0.11.3`; independent `mcp-stateless/uv.lock` resolved 141 packages and installed 137 runtime packages, including FastMCP 4.1.0 and MCP SDK 2.3.0. Final context transfer was 30.03 KB; `deploy/Dockerfile.mcp-stateless.dockerignore` excludes `.env`, secrets, raw_data, indexes, SQLite files, private corpus, artifacts, nested virtual environments and `node_modules`.

Image:

```text
tag: wenjin-mcp-stateless:2026-10-09
id: sha256:1f00cf6f96f22b68302845bad5eb8dbd2e44e976c720edf5029d9849d709fa62
size: 589182086 bytes
```

SHA-256 of the source/configuration used for this build:

| File | SHA-256 |
|---|---|
| `src/wenjin/mcp/stateless_identity.py` | `3e180710dd15c8682c43af5b9350bab5ad106139b6ab7423346243481d0945ed` |
| `src/wenjin/mcp/stateless_server.py` | `bb9f6afbc8952bb4f52f4f3566e1c5fdf083c4b10e3befa7a5e87da765ad8361` |
| `src/wenjin/mcp/stateless_tools.py` | `30ffc42b1832df0adb4dc298d6ffc51c19651427c7b50888ee46a27da96fcbe5` |
| `mcp-stateless/pyproject.toml` | `fc1337e57462b20cbf06acb9de50867de8b1cae903a17d4b75b7cfaae945c449` |
| `mcp-stateless/uv.lock` | `060bc27c009db9c889431156e532c5972d3bb4dde0924fd67e759ef83725cc23` |
| `deploy/Dockerfile.mcp-stateless` | `df5d59157189d459b2166febcdd421a1975310e25fe4ce79f53eac74f3c05693` |
| `deploy/Dockerfile.mcp-stateless.dockerignore` | `7ba9db8174329ea1348ddf07c3526e6efab42c533e010cc369f2110fb20eddef` |
| `deploy/compose.stateless.yaml` | `92ed3d368b7bfc7c6e08d63271684ade42a4712e449c9e7c59f3e19667f6c5eb` |
| `deploy/Caddyfile.stateless` | `e763e99630de9430b7deb9c14477716dfca9a32444aa397a780adcfddd4189c0` |

The exact built image was invoked in a temporary `--rm` container for help, without starting the server listener:

```powershell
docker run --rm --entrypoint python wenjin-mcp-stateless:2026-10-09 -m wenjin.mcp.stateless_server --help
```

It exited successfully and listed the CLI options. This is a short-lived help process in a temporary container; it did not listen for requests or open credentials, `.env`, database, index, corpus, or volume mounts.
