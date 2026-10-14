# 并发基线独立验收方案与源码核查

日期：2026-10-12  
范围：第一轮隔离 Docker 真实 HTTP 并发基线；当前文档只记录静态源码核查和预先约定的验收口径，不包含本轮服务运行或压测数据。

## 当前源码链路

- MCP 入口由 `src/wenjin/mcp/server.py::build_mcp_server` 装配，默认生命周期创建一个 RetrievalService、CorpusRouter、release resolver 和 managed HTTP bridge。项目目标固定为 MCP `2025-11-25` 状态化生命周期；验收必须保留 `initialize`、`notifications/initialized`、能力协商和 MCP session。
- Shared 查询进入 `src/wenjin/mcp/tools.py::search_docs` 后走既有 corpus router / retrieval service。服务与多库 router 共享同一 `search_lock`；`src/wenjin/retrieval/service.py` 在搜索和向量模型调用路径上使用该锁。因此压测结果需标为“包含共享模型锁的端到端吞吐”，并记录模型、候选深度、TopK、查询输入和 warm-up 条件。
- Managed 查询走 `search_managed_file` → `ManagedFileHTTPClient.search_file` → 管理 HTTP API / 唯一 FileManagementService。账户模式从当前 ASGI request scope 取个人 bearer，每次桥接调用单独传 Authorization；身份中间件每个 POST/GET/DELETE 重新验证凭据，并把 MCP-Session-Id 绑定到用户，session 不作为 Principal 或 ACL 缓存。不要把 bearer、session 原值写入报告。
- Managed 服务返回当前 active file/version 身份，MCP 按版本 ID构造下载路径；管理服务查询以 active snapshot 为准，检索期间再核当前版本，身份变化应返回冲突且不带旧证据。并发切换/撤权用例需检查结果里的 `file_id`、`version_id`、library 与正文标记完全匹配预期，不能只看 2xx。
- 下载路由有实例级并发槽位，最多 2 个下载；槽位持有到响应正文发送结束，客户端断开/发送取消时由 `finally` 释放。它与 shared/managed 搜索吞吐应分开报告。

## 必须核验的指标与结果字段

1. **负载定义**：每种场景的 Docker image digest、Compose project/服务/container ID、代码 revision、配置与匿名化 fixture 身份；请求总数、并发度、worker 数、session 数、每 session 并发策略、预热请求数、测量时长、每个查询/`tools/call` 的固定参数。必须明确这是容器真实 TCP/HTTP 流量；TestClient/ASGI、直接函数调用和离线回放不能计入该基线。
2. **时延与吞吐**：按 shared `search_docs`、managed `list_managed_files`、managed `search_docs(file_id=...)` 分别记录成功数、失败数、请求/秒、延迟 p50/p95/p99、最小/最大值；另列状态码和 MCP JSON-RPC 错误分布。只报平均值不足以刻画尾延迟。至少报告 warm 与测量区间；每档并发使用相同输入和请求数，或明确说明负载模型不同。
3. **隔离正确性**：每个响应按调用身份与 session 对账。共享资料检查 corpus/release/target 和证据 locator/hash；managed 检查 file/library/version ID 及可辨识的正文标记。授权用户并发、未授权用户并发、两用户各自 session 混跑、错误凭据、同一 session 被另一用户重用/接管应分别统计。跨用户“没有串身份”需由服务端管理 HTTP请求/审计或可控 fixture 响应证明，不能只凭客户端发出的 token 不同。
4. **快照与竞态**：对于 replace、撤权、delete 若本轮覆盖，应记录触发时序、请求 ID、各并发调用的状态和证据数量。允许明确的旧快照/新快照语义，但不允许旧 version 内容以新 version 标识返回；失败或冲突响应必须 `evidence=[]`。若本轮只做稳态吞吐，明确这些竞态是未测项，不把稳态成功率外推为竞态隔离结论。
5. **取消与可观测性**：区分“客户端断开/任务取消”和 MCP `notifications/cancelled` 协议消息；记录取消发起点、客户端结束时间、服务器观察到取消/断连证据、HTTP bridge 或 retrieval 是否仍完成、审计记录/请求 ID、取消后同 session 和新 session 的下一次请求是否成功。记录取消请求数、确认取消数、取消耗时、残留任务数（测量前后）、卡住连接数及取消后的锁/下载槽位可用性。只有客户端 Future 被取消不能证明服务端已停止工作。
6. **资源与稳定性**：每档并发的 MCP/management/Qdrant 容器 CPU、内存峰值，重启/退出码、连接池/线程池耗尽、SQLite busy/locked、Qdrant/HTTP timeout、连接重置、服务端异常数。用固定窗口与容器统计采样间隔记录，和请求时间轴使用同一时钟源。
7. **隔离与清理证据**：独立 Compose project 名、临时卷/网络/绑定目录清单及 ID；运行前确认 fixture 不指向现有数据目录、共享语料目录和既有 Qdrant；运行前后记录容器/卷/网络清单及 fixture hash。清理后再次 inspect/列举目标资源，证明本轮资源已移除；若保留日志/产物，记录只包含合成数据且没有 token、密码或完整 Authorization header。

## 可复用 fixture 建议

- 优先基于现有 `tests/acceptance/managed_mcp_acceptance_server.py` 和 `tests/acceptance/run_managed_mcp_acceptance.py` 的正式工具链/调用合同，不复用它的 loopback subprocess 结果作为 Docker 基线。Harness 应直接 compose 起独立 MCP + management + 临时 Qdrant，网络从外部 load-generator 通过已发布的隔离端口访问 MCP，MCP 到 management 使用该 project 的私有网络。
- 只在独立临时数据 root/volume 中准备最小、可重复、带明显正文标记的 fixture：一个 shared library/release/target 与一份 managed file 的 active version；replace 用第二个不同正文标记的版本。记录每个 fixture 文件和索引输入的 SHA-256、corpus/release/target/file/version IDs。不要指向工作区当前 `data/`、真实原文、真实 Qdrant 或任何未授权资料。
- 准备至少两名隔离 MCP 用户：A 可读 fixture library/file，B 无权读取；为每人独立签发短期 MCP credential 和独立 session。若第一轮使用旧 Bearer ACL 模式而非账户模式，报告必须说明实际鉴权路径，并将“个人凭据/跨用户 session 绑定”标记为未覆盖。
- 使用确定性、短查询集，每条带 case ID；shared 与 managed 返回不同且唯一的正文标记，便于自动发现串库/串版本。固定单个 embedding/reranker 策略与 TopK；warm-up 单独列出且不纳入统计。性能用例与竞态/取消用例分开批次，避免改变输入或后台 replace 任务污染基线。
- 同 session 并发要作为独立合同：先完成 initialize/initialized 并捕获真实 session header，再用多个 HTTP 请求共享同一 session ID；另设每用户单请求串行的稳定客户端模式，避免把 SDK 客户端线程安全问题误作服务端性能。所有请求保留唯一 correlation ID，但删去凭据和 session 原文。
- 取消夹具应提供可控阻塞点或足够慢的隔离 fixture，不以真实 GPU 睡眠、生产资源或随机大查询制造取消窗口。报告需标明阻塞点发生在 MCP、managed bridge、manager、检索或响应发送哪一段；首轮无可验证服务端取消信号时，只能报告“客户端取消可见、服务端取消未证实”。

## 初步风险与边界

- 该仓库有历史的 synthetic/ASGI 与 TCP 验收，但这些结果不是本轮 Docker 并发基线。必须按新 revision、新临时数据和真实 HTTP 请求独立测量。
- 稳态成功请求只能证明该参数档下的响应和隔离；不能证明 arbitrary concurrency、撤权即时性、替换竞态安全、生产容量或 SLO。
- 查询、凭据、原文/证据正文和稳定个人标识均不进入公开日志。fixture 可以用合成标记；身份仅写匿名标签和短 hash。

## 当前状态

静态核查完成。当前未发现或启动服务，未发送测试请求，未产生模拟/实测流量，未修改业务源码。待 harness agent 交付代码后审查其输入/输出、并发模型、状态机、异常与清理；待主 Agent 提供 Docker 实测产物后，独立复算统计并核验隔离、取消和清理证据。

## 并发分档补充与取消执行约束

主 Agent 提供的第一轮矩阵为 shared/managed 分开、每档 16 请求、并发 1/2/4/8；可作为受控探索矩阵，但每档 p95/p99 只能称为小样本经验分位数，n=16 时 p99 近似最大值，不能据此描述稳定尾延迟或 SLO。若预算允许，增加重复轮次/样本量并报告合并方法；否则保留原始每请求时长并明确样本不足。

当前环境的 FastMCP 同步工具包装调用 `anyio.to_thread.run_sync`，其默认 `abandon_on_cancel=False`；managed bridge 内部是同步 `httpx.Client`（默认 timeout 60 秒）。MCP SDK 对 `notifications/cancelled` 会取消请求 scope、发取消错误响应，但无法强制终止已启动的 Python 同步线程。取消场景必须实际记录客户端响应、线程/manager bridge 完成时间、下游 HTTP 请求数、取消后锁与后续请求可用性；若无法观测线程是否结束，应把服务端停止状态写为未知。

资源采样至少覆盖 MCP、management、Qdrant、model API 各容器的 CPU/内存，采样间隔和请求记录用同一单调时间基准映射 UTC。冷启动与热请求分开报告，并明确定义冷的是容器/进程、模型权重、MCP session、HTTP连接还是 Qdrant cache；若冷样本需要重启或清缓存，分别保存每次启动身份，避免用单一 cold 样本代表分布。主 Agent 已提示 model API 并发 gate 可能立即返回 503，首轮要统计 503 与实际排队/拒绝行为；由于 retrieval search lock 可覆盖搜索调用整段，shared 和 managed 同时请求的碰撞观察应单列，不能将两个独立 batch 的数字推断成同时混合负载。

### Search 锁域的实际边界

进一步核对 `RetrievalService.search()` 与 `FileManagementService.search()`：shared 的主 service 与 MCP 内 `CorpusRouter`共享一把 `search_lock`，锁住完整 `_search_unlocked`；managed 管理进程单独持有 `_rw_lock` 绑定active snapshot，再用自己的 `_model_lock` 包住检索，之后重读snapshot核对 `version_id` 和 raw hash。因为 MCP 与 manager 在 Docker 中是不同进程，这两把Python锁不是彼此竞争的同一把锁。并发 collision 反映远端 model API gate、Qdrant和请求路径组合；应直接观察每个请求的模型API结果、503/耗时及证据身份，不从单次碰撞推断跨进程 mutex。
