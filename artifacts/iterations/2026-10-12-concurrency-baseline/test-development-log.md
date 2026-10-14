# Harness 开发记录

日期：2026-10-12

## 处理记录

- 首次 `prepare`（run `20261012T064235Z-824b0b`）生成的 Compose overlay 含重复 `management` / `mcp` service key，`docker compose config --quiet` 拒绝配置。该失败只创建了隔离临时目录、状态文件和 overlay；未启动服务、容器或测试请求。
- 修正 overlay 生成器：每个 service 只生成一个 mapping，并加入重复 key 检查。用现有失败状态重新生成 overlay 后，Compose config 静态验证通过。
- 新 `prepare`（run `20261012T064619Z-0ecebe`，project `wenjin-concurrency-a7278c78`）再次通过 `docker compose config --quiet`，记录 `services_started=false`。当前 Compose project 未启动。
- 管理员初始化通过 `IdentityStore` 写入本次临时 `identity.sqlite`，随后用 HTTP login 验证；不经过 Docker NAT 调用 loopback-only bootstrap endpoint，不修改业务 guard。
- 失败请求也记录 client latency、HTTP/MCP status 分类；scheduled time 与 wire start 分开，逐请求追加到 `requests.jsonl`。全请求与成功请求延迟、总请求速率与成功速率分别统计。
- 首轮执行期间新增独立 `controlled_scenarios.py`，供后续同次服务生命周期调用：碰撞按共享/托管各自 fixture 身份断言；托管文件 replace、撤权/无证据、regrant、delete/404 顺序合同。未添加服务端取消已停止工作的断言。

## 当前窄验证

- `uv run --no-sync ruff check artifacts/iterations/2026-10-12-concurrency-baseline/run_concurrency_baseline.py`：通过。
- `uv run --no-sync python -c "from pathlib import Path; p=Path('artifacts/iterations/2026-10-12-concurrency-baseline/run_concurrency_baseline.py'); compile(p.read_text(encoding='utf-8'),str(p),'exec')"`：通过。
- `prepare` 与 Compose 静态 config：通过；该步骤没有启动服务或产生业务流量。
- 当前等待主 Agent 运行 `--single-only` 的真实隔离 HTTP 流程；并发矩阵、撤权/替换/删除/取消合同仍待验证或明确标注未覆盖。
- 主 Agent 正在执行 `--single-only`；runner 当前保持不变，避免修改在途进程使用的脚本。Acceptance 静态复审发现 session close 幂等性与 initialize 后 tools/list 失败时清理 session 的补充问题，计划首轮结束后修复并用 MockTransport 窄测。

- 首次真实 Docker 流程在 identity A/B 并发 probe 处因 `Benchmark.reader_token` 赋值过晚而失败。主 Agent确认镜像、model/Qdrant、真实远程向量 fixture、管理 HTTP 和 default-deny 已完成；该错误属于 harness，隔离 project 随后清理到 0 容器/卷/网络。修复为 reader 创建后立即绑定 MCP token，并在失败清理前写安全阶段/堆栈帧检查点。
- 修正后 `check_concurrency_harness_offline.py`：3/3 通过（统计失败响应、SSE、双账号 MCP session/DELETE）；Ruff 与 Python compile 通过。
- 新终版 `prepare` run `20261012T065621Z-55dc61` 的 Compose 静态校验通过，未启动服务；完整 run 由主 Agent执行。
- 后续真实流程定位第二项 harness 假阴性：shared evidence schema 使用 `index_target_id`，原断言误读 `target_id`，因此授权 A 命中也触发 identity fail。现已统一 response evidence 检查/导出字段为 `index_target_id`，MCP 请求仍发送 `target_id`；A/B 权限 probe 在断言前逐case写入合成 RPC 与 evidence identity，避免错误时丢失证据。
- 修正后离线检查 3/3、Ruff、compile 通过；新 `prepare` run `20261012T070154Z-2947fe` Compose config valid、未启动服务，等待主 Agent重跑。
- run `070154` 的 shared 首请求真实 HTTP 成功后，`summarize()` 因统计器仍读取旧 `start_utc` 字段失败；请求 JSONL已有 0.477 秒成功行，隔离资源清理完成。现改为跨行取 `wire_start_utc` 最小值和 `end_utc` 最大值。
- 同轮还把 managed measured evidence 的 `version_id` 增加对 fixture 预期版本的直接比较；新增 Mock `run_level → summarize` 字段集成和旧版本误报测试。离线检查 6/6、Ruff、compile 通过。
- 新 `prepare` run `20261012T070604Z-2f798c` Compose config valid、未启动服务，供主 Agent继续真实 full run。

## controls-only harness revision — 2026-10-12

- Added `run --controls-only` to repeat isolated fixture/account setup, one request per link plus warm-up, initial concurrent A/B authorization check, managed lifecycle probes, and cancellation probe while skipping the concurrency matrix and collision.
- Lifecycle now journals sanitized management responses, MCP RPC/evidence identities, operation times, and final job objects. Replacement failure remains an observation and does not short-circuit revoke/regrant/delete checks. Delete accepts the service's terminal `deleted` job state and verifies the retained tombstone plus a fresh MCP denial/no-evidence response.
- Replacement, revoke, and delete each start an independent MCP search before issuing the management action; reports label this as overlap only when the request was scheduled first, and retain the request result separately from a later independent verification request.
- Cancellation captures non-200 HTTP bodies and SSE parse failures, closes the original MCP session in `finally`, then opens a new session for the follow-up search. Downstream synchronous work termination remains unknown.
- Validation: `py_compile` for runner and controlled scenarios; `check_concurrency_harness_offline.py` 6/6 passed; `run --help` exposes `--controls-only`. No Docker services or business workload started by this revision.
- Acceptance review follow-up: controls-only also records one controlled shared+managed collision with full sanitized MCP RPC/tool error/HTTP category/raw body; protocol payloads use sensitive-key and known-token redaction. Collision is exploratory and does not treat a managed tool error as a successful busy rejection.
