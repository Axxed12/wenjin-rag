# Remote identity bridge validation

This check covers the newly authorized 2026-07-28 stateless MCP identity bridge. It does not change the existing stateful 2025-11-25 entry or project narrative.

## Review result

The management endpoint authenticates the supplied personal MCP Bearer on every request by calling `resolve_mcp_access_snapshot(token)`. The store validates the token, current account status, active resources, role, and grants within one read transaction. The response projects only active resources and managed files that the resulting `AccessSnapshot` allows; it does not return grants, denied-resource metadata, other-user data, or token hashes. The response is marked `Cache-Control: no-store`.

The remote MCP adapter sends the request's original Bearer for each HTTP authentication call and keeps no principal or access snapshot cache. Invalid/revoked credentials become 401; bridge/network/protocol failures fail closed as 503 before the MCP request reaches tool handling. Remote mode is selected explicitly by `--identity-mode remote` or `WENJIN_MCP_IDENTITY_MODE=remote`; it rejects simultaneous local identity database or static API-key configuration. The default remains local mode. The remote HTTP client is closed with the stateless server lifespan.

An independent contract file, [test_remote_identity_validation_contract.py](../../../tests/test_remote_identity_validation_contract.py), adds valid-cookie rejection, same-account independent Bearer credentials, next-request revocation, 401/503 fail-closed handling, and static-key fallback rejection. The backend's [test_remote_identity_bridge.py](../../../tests/test_remote_identity_bridge.py) covers exact file deny over library allow, library grant removal, credential revocation, disabled/must-change accounts, admin access, and omission of unauthorized active resource IDs and token hashes.

## Validation evidence

The following command ran in the workspace's default environment:

```text
uv run pytest -q -rs tests/test_remote_identity_bridge.py tests/test_remote_identity_validation_contract.py tests/test_mcp_stateless_identity_release.py tests/test_mcp_identity_http.py
9 passed, 1 skipped, 5 warnings in 5.31s
```

The skipped module, `tests/test_mcp_stateless_identity_release.py`, explicitly requires FastMCP 4.x and MCP SDK 2.x; the default environment has FastMCP 3.4.4 and MCP 1.29.0. The skip is not counted as a pass. The stateful identity HTTP test did run. A scoped Ruff check over the changed bridge, middleware, endpoint, tool, download, and contract files passed.

The rebuilt disposable image `wenjin-mcp-stateless:2026-10-14` (`sha256:eb09d2aecf993e54cbcf08b556139b8f30fe25b40b3088997799b168105cddc6`) reported FastMCP 4.1.0 and MCP SDK 2.3.0. Running [remote-identity-rpc-synthetic.py](remote-identity-rpc-synthetic.py) with `docker run --rm` completed a FastMCP 4 JSON-RPC `server/discover`, `tools/list`, and `tools/call:list_corpora` flow. The MCP app ran through its ASGI HTTP stack; the identity adapter reached a temporary Uvicorn management `create_app` over loopback HTTP. It returned exactly one synthetic authorized corpus. The script created its identity database and corpus manifest under a temporary directory, mounted only the script read-only, emitted no credential, and exited with the container. The executed command was:

```text
docker run --rm -v "E:\日常任务\rag-project\artifacts\iterations\2026-10-14-mcp-entry-control-ui-deploy\remote-identity-rpc-synthetic.py:/tmp/remote-identity-rpc-synthetic.py:ro" wenjin-mcp-stateless:2026-10-14 python /tmp/remote-identity-rpc-synthetic.py
```

Ruff command:

```text
uv run ruff check tests/test_remote_identity_bridge.py tests/test_remote_identity_validation_contract.py src/wenjin/mcp/remote_identity.py src/wenjin/identity/mcp_auth.py src/wenjin/identity/store.py src/wenjin/management_http/identity_http.py src/wenjin/management_http/http_common.py src/wenjin/management_http/app.py src/wenjin/mcp/stateless_server.py src/wenjin/mcp/tools.py src/wenjin/mcp/download.py
All checks passed!
```

## Boundary

All account, token, grant, and corpus data used here was synthetic and stored under temporary roots. No existing identity database, grants, tokens, or running service was changed. The disposable image exercised the new protocol and the real loopback management HTTP authority, but it did not mount project indexes or prove cross-OS production deployment, WAN/TLS, or the root-owned live preview. The default-environment stateless release test remains unexecuted because that environment has the older SDK; the FastMCP 4 RPC smoke covers discovery and authorized corpus listing, not retrieval quality or source download.

## Parser-alignment image/profile review (2026-10-14)

A read-only review of `deploy/Dockerfile.mcp-stateless`, `mcp-stateless/uv.lock`, and `deploy/compose.stateless.native-identity.yaml` found:

- The Dockerfile's lxml 6.1.3 sdist URL and SHA-256 exactly match the source artifact in `mcp-stateless/uv.lock`. The dedicated builder creates a wheel from that checked sdist; the final stateless venv first uses `uv sync --locked`, then replaces only lxml with `uv pip install --no-deps --reinstall`. Build-time checks require both `etree.LIBXML_VERSION` and `LIBXML_COMPILED_VERSION` to equal `(2, 11, 9)`. The default `deploy/compose.stateless.yaml` image tag remains `2026-10-14`; the new `2026-10-14-libxml-2.11.9` tag is used only by the opt-in mixed profile.
- `docker compose -f deploy/compose.stateless.native-identity.yaml config --quiet` passed when required secret *path variables* were set to validation-only placeholders. Running it without those variables failed closed as expected. This command validates Compose syntax/interpolation only; it does not read token contents or start services.
- The profile keeps the existing stateless hardening: image UID/GID 10001, read-only root filesystem, `/tmp` tmpfs, `no-new-privileges`, all capabilities dropped, loopback-only port 18768, read-only corpus/config/index/release/raw mounts, remote identity mode, and model/Qdrant Docker secrets loaded by the existing entrypoint. It does not mount an identity database or the host's general project directory.

The replacement image subsequently completed; its exact rootfs/runtime checks and the synthetic RPC result are recorded below. Those checks do not establish six-chunk source-replay recovery or real corpus acceptance.

## Rebuilt parser image verification (2026-10-14)

The completed image is `wenjin-mcp-stateless:2026-10-14-libxml-2.11.9`, image ID `sha256:6c9ef8447bac7f4159ad56d93bff484db845dbda12e20f8ebca54eacf0efcc72`. A `docker run --rm` check used `--network none`, `--read-only`, and no data or secret mounts. It observed UID/GID 10001, lxml 6.1.3, libxml runtime/compiled 2.11.9, and libxslt runtime/compiled 1.1.39. `gcc`, `cc`, `g++`, `make`, `pkg-config`, `build-essential`, and `libssl-dev` were absent. No proxy environment variables, `/run/secrets`, identity database, temporary lxml source inputs, or wheelhouse appeared in the final runtime view.

A second read-only inspection found a material exception: `/root/.cache/uv` contains 181 files totaling 11,727,267 bytes, including lxml `.pyx`/`.pxi`/header source and compiled `.so` artifacts. Therefore the final image does retain a non-empty uv build/package cache; the “no source cache” check failed. This appears to come from the final-stage `uv pip install --reinstall` running without the cache mount used by `uv sync`. This image should not be described as free of build/source cache until rebuilt and rechecked.

The build helper `deploy/prepare_lxml_build.py` validates SHA-256 for five local source archives, extracts the pinned lxml sdist, and modifies only the extracted Python `buildlibxml.py` download helper functions so they use the verified local libxml2 2.11.9 and libxslt 1.1.39 archives. It does not modify parser C sources. The lxml source URL/hash matches the locked lxml 6.1.3 sdist. The Windows environment remains on its existing libxslt 1.1.45; this image intentionally uses 1.1.39 to build with libxml2 2.11.9.

The synthetic HTTP JSON-RPC helper also passed on this new image with `docker run --rm --network none --read-only`, mounting only the helper script read-only and a temporary `/tmp`: `server/discover` and `tools/call:list_corpora` succeeded, one synthetic authorized corpus was visible, and the current Bearer reached the temporary management authority. No project corpus/index, account database, or secret was mounted. This remains a synthetic protocol/identity smoke; it does not prove real corpus admission, parser replay equality, or deployment acceptance.

## Cache-fix image revalidation (2026-10-14)

After adding a BuildKit cache mount to the final-stage `uv pip install`, the same isolated rootfs checks were rerun against the rebuilt tag. The current image ID is `sha256:74ca2f90a09b0aa492d0971d72ef6659792d372121f08dda0edeea6968ff7a03`.

The final rootfs check now reports `/root/.cache/uv` absent (0 files, 0 bytes). The lxml static input/source/wheelhouse paths, `/run/secrets`, and `/var/lib/wenjin/identity.sqlite` are absent; build packages and compiler executables are absent. A default-user no-network/read-only container again reported UID/GID 10001, lxml 6.1.3, libxml runtime/compiled 2.11.9, libxslt runtime/compiled 1.1.39, and no proxy environment variables.

The synthetic HTTP JSON-RPC helper passed again on this image with `--rm --network none --read-only`, a read-only mount of the helper script, and a temporary `/tmp`: `server/discover` and `tools/call:list_corpora` succeeded, with one authorized synthetic corpus and the Bearer forwarded to the temporary authority. No project data, identity DB, or secret was mounted. The checks did not start or stop the running profile and do not claim the root-owned real-asset admission audit result.
