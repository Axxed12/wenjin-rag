# 模型推理有界准入与错误传递

## 状态

2026-10-12：业务接线、静态检查和本轮独立 scoped 验收完成。修前失败依据仍保留在[并发基线报告](../2026-10-12-concurrency-baseline/final-report.md)及原始run日志，没有覆盖为修后证据。

## 问题与处理

- 修前 shared 与 managed 并发竞争同一模型时，下游模型API返回503，managed管理桥继续透传，但MCP工具最终呈外层成功、零证据。基线中混合碰撞只有一次，不能据此估算失败率或生产容量。[来源：并发基线最终报告](../2026-10-12-concurrency-baseline/final-report.md)
- 新增`model_api/admission.py`单worker FIFO准入，共用在query/document embedding与rerank单次调用上；FIFO公平性只针对单次模型调用入队序号，不保证整条query原子公平。每个模型API进程/副本独立持有一个worker，副本之间没有共享队列或统一仲裁。`max_waiting`表示active以外等待数，满队`model_queue_full`、等待超时`model_queue_timeout`均为503并分别给中文detail。队列容量默认8、最大等待30秒；配置范围等待数0–128、时间>0且不超过300秒，拒绝非有限等待值。
- idle请求先占active reservation；实际worker准备执行前才标记started。取消/断开会移除queued或未启动reservation；已启动操作由worker持有到底层同步线程完成后才释放槽。每项有单调时钟绝对deadline，worker分派时再次核验。shutdown停止接单、立刻以稳定shutdown码拒绝queued/未启动reservation、等待started操作drain后再关闭模型runtime。
- HTTP层在body读完后轮询`Request.is_disconnected()`，避免只依赖ASGI自动取消；已断开的排队操作取消并移除，active调用由worker继续完成。保留runtime `RLock`，作为同步运行时防线；不增加GPU并发、Redis、缓存、检索参数或自动重试。
- 模型identity加入active/等待容量/等待时间策略，implementation fingerprint包括admission.py。Remote model API read默认90秒并读取服务identity校验read预算大于排队预算，connect/pool分别配置；管理桥read默认210秒且connect/pool独立，readtimeout使用`management_bridge_read_timeout`，与模型读取超时和queue timeout区分。210秒不是整条查询SLA，多库模型调用和管理锁未设总时限。
- 配置审查发现bridge read被覆盖成短于remote model read时，管理HTTP桥可能先超时。补充remote模式启动配置守卫：模型read值须有限且在0–300秒范围内，bridge read必须严格大于该模型read；配置错误只给安全中文原因，不回显输入值。local模式不强制remote read预算；没有新增identity请求或自动重试。该守卫只保证桥预算大于单次模型HTTP read预算，不保证多阶段query完成。
- provider到管理HTTP维持队列稳定错误码、中文detail和`evidence_status=not_retrieved`；MCP检索工具将队列或管理桥read超时包装为`ToolResult(is_error=True)`与`structured_content`，避免把失败伪装成正常空结果。未改MCP 2025-11-25状态化协商和会话协议。

## 检查与证据

- 开发侧静态检查：全改动静态检查及本次守卫后的`managed_client.py` Ruff、格式检查、compileall均通过；本轮开发未运行测试runner，测试由独立负责人执行。
- 文档检查：`code.md`为55行，修改Markdown的本地链接均可解析。
- 独立测试负责人最终报告：准入/配置/HTTP/MCP合同24项通过；remote provider与managed bridge既有回归另为24项通过，这是不同测试批次，不相加为48种覆盖。stateful MCP Docker wire probe成功返回`isError=true`、结构化`model_queue_full`与`evidence_status=not_retrieved`。[独立测试记录](test-development-log.md)
- GPU/Docker targeted run `20261012T084409Z-6be28c`通过：单shared、单managed检索、各身份授权/default-deny、shared+managed混合并发各返回预期marker及identity、stale-version拒绝无证据；合成文件完成ingest后，经MCP读取到新marker且file/version身份吻合。三服务attestation均与冻结源码SHA一致。[结果](runs/20261012T084409Z-6be28c/targeted-results.json)、[manifest](runs/20261012T084409Z-6be28c/run-manifest.json)。
- query与ingest客户端操作区间重叠0.609秒，但未记录服务端准入时间、队列长度或active inference；不能据此声称真实GPU模型竞争或排队。query/document/rerank共用队列的资源生命周期由确定性fake-backend CPU合同覆盖。shutdown/排队断开语义也有CPU/TCP合同；MCP通知取消后双响应异常仍在后续范围。
- 本轮run清理后临时项目容器、卷、网络均为0，临时root和凭据已删除，run后源码哈希仍匹配。[清理记录](runs/20261012T084409Z-6be28c/cleanup.json)。未跑全量测试、128请求并发矩阵或冻结评测集，不构成生产验收。
- 修后源码SHA-256为`c0aa7dfcdb151c0f7f957a66b889bb3ba1406c4ffa1f3d5e671d6b7154f5a674`；管理、MCP、model-node三服务source attestation均匹配该值。模型asset及compose配置身份见[run manifest](runs/20261012T084409Z-6be28c/run-manifest.json)和[冻结记录](final-source-freeze.json)。

## 当前选择与下一步

8个等待位、30秒服务端等待、90秒模型read、210秒管理桥read和5秒connect/pool是本轮暂定初值，不是用户确认的最优值。targeted run只验证这些实际配置及守卫，并未遍历部署环境覆盖值或确定容量。下一步保留对部署配置覆盖值的单独核验，并把修前基线发现的MCP通知取消双响应问题作为独立后续任务；当前未观测多库共享/受管查询的全链路总SLA，也未验证多副本准入协调。

## 目录变化

- 新增：`src/wenjin/model_api/admission.py`、本目录开发记录。
- 修改：`src/wenjin/model_api/server.py`、`src/wenjin/model_api/__init__.py`、`src/wenjin/settings.py`、`src/wenjin/models/providers.py`、`src/wenjin/management_http/http_common.py`、`src/wenjin/mcp/managed_client.py`、`src/wenjin/mcp/tools.py`；Compose配置模板、`.env.example`、`docs/dual-deployment.md`、`code.md`、`docs/development-log.md`。
- 测试文件及测试日志由独立测试负责人维护，不属于本开发记录的写入范围。

仓库现有`.gitignore`的`models/`和`*.md`规则会忽略`src/wenjin/models/providers.py`及本轮Markdown文档；以上仍是实际编辑路径，按要求未扩大或重写ignore规则。
