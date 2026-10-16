# 2026-10-14 MCP entry control Windows deployment review

## Decision

The Windows hybrid boundary is technically reachable for the native management API and existing model/Qdrant services. The broker can be deployed separately on a loopback-only host port and a dedicated internal Compose network; `/root` is preparing the broker-only Compose profile and will execute any live start/reload. I did not start, stop, rebuild, or reconfigure an existing service.

Do not deploy the stateless MCP container against the current Windows identity database yet. Synthetic cross-platform WAL testing found a concurrent access failure: while a Windows SQLite writer held a WAL connection, the Linux container's writable directory bind failed in `StatelessIdentityStore` with `sqlite3.OperationalError: disk I/O error`. A read-only bind could see a newly committed WAL row only while sidecars already existed; the same read-only test failed with `unable to open database file` when `-wal` and `-shm` were absent. This is not a safe basis for live credential or grant checks. The 2026-07-28 stateless protocol is authorized as an isolated iteration, but its runtime deployment remains blocked on a reliable identity-store boundary.

## Topology checked

- At the start of this review, native management listened on `127.0.0.1:18777` and native legacy MCP on `127.0.0.1:18765`; both use the existing project root and identity database. Docker contained the model and Qdrant services only. No broker or stateless MCP service was running.
- The Docker control broker uses a fixed Compose project and fixed service mapping (`legacy -> mcp`, `stateless -> mcp-stateless`). Since the native legacy process is not a Docker container, it must remain reported as unmanaged/not found. There is no stateless container target until one is safely deployed.
- The broker should be reachable from the native management process at `http://127.0.0.1:18769`, published only on loopback and attached only to a dedicated internal control network. Only the broker receives the Docker socket. The exact broker profile and live command are being prepared by `/root`.
- A stateless container can reach host services when started with `extra_hosts: ["host.docker.internal:host-gateway"]`. From a disposable container using the newly built image, `/api/health` on host port `18777` returned HTTP 200, the model port `18766` accepted TCP, and Qdrant `/healthz` on `6333` returned HTTP 200. This proves network reachability only, not authenticated model use or MCP business readiness.

## Image and path probes

Built isolated local images; neither was started as a service:

- `wenjin-mcp-stateless:2026-10-14` — `sha256:1b3e1df87fba57eb3cfd09b63296256ce4fe341651d4037a78d493972bc9ee34`
- `wenjin-backend:2026-10-14` — `sha256:6941765e2224efa160aa01e5a2518563d6c2bd9f3b93a2940ce33a90215d37cd`

The stateless image mounted the existing `corpora`, `corpora-private`, `configs`, `data/indexes`, `data/releases`, and `raw_data` directories into Linux paths with read-only bind mounts. The container read the active registry entry, SQLite schema metadata from the release catalog and BM25 index, and counted manifests/config directories. This verifies the current Windows path mapping and read-only access for those assets; it does not start retrieval or validate Qdrant/index admission.

The existing identity database resides under `data/file-management`, alongside management databases and file-management state. A writable bind of that parent would grant the MCP container filesystem access beyond the identity DB. A read-only bind avoids that write scope but cannot reliably create SQLite WAL sidecars. The WAL experiment used a disposable, synthetic database and did not read, copy, or write the real identity database.

## Evidence boundary and next step

No real corpus contents, production identity/grant rows, manager database, or Docker socket were changed by this review. No existing listener or container was started, stopped, or reconfigured. Image builds, host-gateway connectivity probes, read-only asset mounts, and synthetic SQLite tests are local preparation evidence, not deployment or product acceptance.

The broker-only management-control change can proceed independently. Keep the stateless entry visible as not deployed/not found until the identity access boundary is resolved and separately tested. Do not point the container at the current `data/file-management` directory merely to make startup succeed.
