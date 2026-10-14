# Harness 离线定向检查记录

时间：2026-10-12（本地运行；无服务请求）。

## 验证范围与命令

```powershell
.venv\Scripts\python.exe artifacts/iterations/2026-10-12-concurrency-baseline/check_concurrency_harness_offline.py
```

脚本通过 `httpx.MockTransport` 模拟 initialize、initialized、tools/list、tools/call 与 DELETE；另以 16 条合成记录复算 nearest-rank 分位数和成功吞吐，并验证 SSE `event/data` 与 CRLF 解析。脚本未调用 Docker、模型、真实 MCP/管理HTTP服务，也未改业务源码、volume 或运行数据。

## 结果

首个版本的 tools/list 单项检查直接调用 `structured_tool_result()`，得到 `_invalid_result`。复核后确认该 helper 只负责 tools/call结果；`McpHttpSession.call()`按 `method == "tools/list"` 读取 JSON-RPC 顶层 `result.tools`。因此首次失败是检查对象选错，不构成 harness 当前实现的失败。检查脚本已改为通过 `McpHttpSession.call("tools/list", ...)` 观察实际分支。

修正后的离线检查为 **3 passed**：

- 16个已完成HTTP响应含1个503：成功15、失败1、失败率6.25%、窗口4秒的成功吞吐3.75/s；响应时延经验分位仍包含有 `latency_seconds` 的503；16样本时p99等于最大样本并标注为探索性小样本。
- SSE解析读取首个 `data:` JSON payload。
- 两个个人token分别建立独立MCP session；每个初始化、initialized、tools/list、工具请求和 DELETE 都携带各自token/session，关闭后MockTransport客户端均关闭。

执行时 harness SHA-256：`B9F19048226B4E41D5F733E1B9A7C97550C7AF02483BEBC70F6B8FAD1C3024AA`。检查脚本 SHA-256：`3279B3B2B518D832350AAA090949B95F26885257EBA3AC8E3263EF15F07DE842`。Harness 仍处代码审查阶段；本结果不代表 Docker Compose 配置、容器加载当前源码或真实HTTP并发已通过。最新源码改动后需重跑本离线脚本。

## 待修复/复核

复核当时版本发现 Compose overlay 重复定义 `management` / `mcp` keys；敏感 secret 文件在 down 前被删除；尚无容器内 `wenjin` 实际导入路径/源码hash证明；模型目录hash一次读整文件；reader B 授权拒绝 probe 是串行、不是 A/B 并发隔离；shared/managed collision 未逐响应检查证据标记和身份。以上已直接反馈 harness agent，修复后复审；未执行准备或live命令。

## Harness迭代复查（14:xx，代码继续变化）

Harness随后增加成功请求独立分布、HTTP/MCP状态统计及A/B并发授权对照；离线统计断言同步覆盖 all-request 与成功-only时延分布，16条中1个HTTP 503在全请求p99中计入最大值、成功only p99排除该503，成功吞吐仍为15/4秒。当前复跑为 **2 passed / 1 error**：统计与SSE通过；session生命周期检查因 harness 正常路径先关闭session、`finally`再关闭一次，第二次 `close()`在已关闭的`httpx.AsyncClient.delete()`抛 `RuntimeError: Cannot send a request, as the client has been closed.`。幂等关闭已反馈 harness，待修复后重跑。

## 幂等关闭修复后的复跑（14:xx）

Harness 修复 `McpHttpSession.close()` 幂等状态，并将 `initialize` 成功后从 `notifications/initialized`、`tools/list` 到失败清理纳入关闭路径后，复跑原离线 MockTransport 脚本结果为 **3 passed**。统计、SSE 和双身份独立 session 生命周期均通过；无 Docker、网络服务、真实HTTP请求或业务代码改动。此前2/3失败保留为修复前回归证据，不删除。

当前仍是 harness 离线测试，不能证明镜像容器启动、Docker真实HTTP、工作源码实际导入或压测结果。独立审阅另发现 `run_collision()`将任意HTTP 503直接分类为`model_busy_rejection`，而`McpHttpSession.call()`对非200不保留响应正文；建议仅在可识别的模型busy错误码时判作预期拒绝，否则记为未知失败。

## run_level / summarize 集成检查及managed预期版本断言复跑（15:xx）

受主 Agent 请求，增加一条全离线假客户端检查，真正调用 `Benchmark.run_level()` 生成 request rows 再进入 `summarize()`，覆盖一次 HTTP200 成功和一次 `httpx.ReadTimeout`：请求行保留正向200与 timeout `http_status=null`，timeout仍有 client latency，状态计数只计已收到HTTP的200，摘要时间窗取第一条`wire_start_utc`至最后一条`end_utc`。Harness 同时加入 `managed` 证据版本必须匹配 fixture 的离线反例测试（返回旧版本应失败），及时间戳路径测试。

`.venv\\Scripts\\python.exe artifacts/iterations/2026-10-12-concurrency-baseline/check_concurrency_harness_offline.py` 当前 **6 passed**。测试仅使用临时目录和FakeSession/httpx MockTransport；没有Docker或业务服务流量。脚本SHA-256以执行后输出为准。

此次离线检查发现并推动修复了live run 070154的摘要错误：原 runner rows 采用`wire_start_utc`，summary仍访问不存在的`start_utc`，运行在第一条shared请求后因KeyError退出。相同run的request row留有1条首请求HTTP200、标记/target匹配、0.477056秒，但没有managed、warm或矩阵结果；不能视为并发基线。临时Compose项目的down与容器/卷/网络清理检查通过。另有run 065621因验收代码读取`target_id`（实际evidence字段为`index_target_id`）错误中止；修复后的身份探针在070154通过，详见最终独立报告。

本次离线检查脚本SHA-256：`51F29E50AFE06A39D8F353C89A44C5C1BE8A47BC22039DD6E01F47DCD40F97E3`。
