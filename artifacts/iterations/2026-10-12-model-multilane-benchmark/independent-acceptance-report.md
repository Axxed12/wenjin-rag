# 单模型多路并发实验独立验收报告

日期：2026-10-12  
状态（最新）：1-lane reference、2-lane 240-request三类workload、1/2-lane HTTP cancellation boundary均通过独立原始复算；2-lane存在CUDA interval overlap且相对1路三类phase吞吐提升约10–24%。因GPU/RAM余量与资源采样不支持安全预测更高并发，4-lane本轮由安全预算主动暂缓、未启动或测量。MCP 1-lane run `20261012T125703Z-2d27c2` 的35次tool call通过原始复算；MCP 2-lane run `20261012T125923Z-b93726` 在MCP流量前因`model_identity_invalid`启动失败，不是MCP行为/性能样本，尚无1/2 MCP对照。验收者只读复算，未启动GPU、服务或HTTP流量，也未修改业务源码。

## 立即结论

**生产实现保留 `inference_lock`；隔离实验可使用经批准的 guarded adapter 验证 1/2/4 路真实 forward overlap。**模型 API 默认 admission 单 worker，runtime 通过 `RLock` 包住 embedding/rerank。对 cached image 的逐版本源码和固定权重 config 只读核查后，已确认可在独立 benchmark copy 中限制模型到固定 device/eval、序列化 tokenizer/preprocess、让共享权重只读，并以独立 CUDA stream 驱动 forward；close/drain 仍受保护。详细范围及拒收条件见[缓存运行时源码复核](cached-runtime-source-review.md)。这只是受限实验授权，不是生产线程安全结论。

判定真实 GPU overlap 必须依赖服务端 queue/worker/forward 事件及每 lane CUDA start/end event。多 worker admission 或 CPU forward 区间不能单独证明 GPU overlap。若 guard、版本、权重/参数/buffer 指纹或 1-lane 等价性任何一项不满足，应拒收该轮。

## 最新独立复核（2026-10-12）

### 边界轮 124331Z-e77826

独立读取 `runs/20261012T124331Z-e77826/boundary-summary.json`、`model-events.jsonl`、`outcome.json` 与 `cleanup.json`。11 个 HTTP 请求、144 条服务端事件；队列满、等待超时、排队取消、CPU active disconnect、GPU active disconnect 五项均通过。排队取消原始序列为 receive→enqueue→queued_cancel→HTTP complete。GPU 请求的 `forward_dispatch`（102865257883588）早于 `active_disconnect`（102865297427306）；12 个 CUDA `forward_end` 均发生在 disconnect 后、`operation_exit`（102866074917707）前，之后才 `lane_drained`（102866075995783）。断连后的新 embedding 请求返回有效 200 向量。formal/frozen/model state unchanged 均为 true；cleanup 的容器、卷、网络为0且临时目录已移除。此结果证明该隔离实验路径没有因客户端断开而提前释放 active slot；它不是通用模型线程安全或生产验收。

纯 ASGI trace wrapper 的 cached-image HTTP mock 合同也已独立复跑：`wenjin-backend:dual-smoke-20261008`、`docker --network none`、实验源码和合同脚本只读挂载，health、embedding、rerank 请求及关联事件均通过。该 ASGI 合同本身不证明 TCP disconnect；上段边界轮原始事件才提供真实 HTTP 证据。Wrapper 当前对 request headers 使用 `dict(...)`、对响应无条件追加实验 request-id；本轮客户端只发一个该请求头且响应 handler 不自行添加，因此不影响本轮结果；多同名头的规范化不在本轮覆盖范围。

### 三类 workload 程序静态审查与当前单路运行

当前 workload SHA 已绑定 operation kind、分组、阶段顺序、请求数、轮数和混合分配规则：每轮纯 embedding 32 条、纯 rerank 16 条、mixed 32 条（8个 wave，每 wave 2 embedding + 2 rerank），3轮合计240条。family/round 和 group/round 指标通过原始客户端请求起止时间窗计算成功/完成吞吐，并记录统计中位数、nearest-rank P50/P95/max与样本数；CUDA interval overlap 由服务端 CUDA event 计算。Harness报告38项CPU合同、Ruff与compile通过；reference identity/workload在发请求前验证。上述均是程序合同或静态结论，不是推理表现。

最终单路 run `20261012T124501Z-fe800f` 已独立复算并通过：240/240 unique request IDs均HTTP 200、0 errors；纯 embedding/纯 rerank/mixed分别96/48/96，与预期相同；server events齐全、ID/status correlation通过、24/24 mixed waves有跨 operation HTTP重叠。242项freeze hash均匹配，source SHA `151fbd6e…a994f` 与边界轮一致，formal source SHA `c0aa7dfc…a674`；单一模型容器/image、权重只读、模型状态/frozen/formal source结束后不变。Reference与原始六组同输入输出逐项复算：embedding维度匹配模型identity、向量norm范围0.9999999944–1.0000000621、对reference最大L2及max-abs差均0、最小cosine为0.9999999999999998；reranker候选数匹配、score max-abs差0，故名次及Top-k一致。CUDA事件峰值同时活动lane=1，同lane forward区间无重叠。实验隔离reference只适用于当前冻结版本和负载。

底层实际forward调用数按模型事件而非HTTP input列表统计（总请求240不等于240个forward）：query-short/query-long各144；document-short/document-long各36；4候选rerank为96；8候选rerank为192；另有warmup产生16个forward events。此差异符合query批量接口拆成逐文本forward、document和rerank按各自路径/batch处理；后续各lane报告须保留每请求及每组forward计数。

吞吐独立复算使用每个连续family phase所有raw client `request_started`到最后 `response_completed`的时间窗：纯 embedding 96/16.203s=5.925 successful req/s，纯 rerank 48/7.469s=6.427 req/s，mixed 96/15.922s=6.029 req/s。client E2E统计中位数/nearest-rank P50/P95/max分别为：embedding 398.73/394.91/857.91/1093.04 ms，rerank 365.46/363.10/797.26/857.76 ms，mixed 417.49/417.21/816.12/942.17 ms；样本数分别96/48/96。统计中位数对偶数样本取中间两项平均，故不一定等于nearest-rank P50。按原始单组单轮请求窗另算三轮结果。程序 `duration_stats_by_family_round` 中family同名轮包含多组按组外层顺序执行的请求，时间窗会跨越其他组/轮；不将其作为独立round RPS，2/4比较应继续使用连续family phase总吞吐及group-round raw时间窗。

资源硬阈值：GPU free停止线1024 MiB（起始门槛8192 MiB）、宿主可用RAM停止线3072 MiB（起始门槛6144 MiB）、容器连续重启停止线2。本轮GPU free最小4777 MiB、宿主free最小4293 MiB，未触发停止；GPU利用率最大100%，model-node CPU采样最大248%（当前阈值文件未配置CPU硬停止线）。宿主/GPU采样179条，间隔median/max 563/641ms；Docker 23条有效容器采样，median/max 4468/4750ms，名义间隔2s，因此容器短时CPU/内存峰值的观测分辨率较粗。通过状态、formal/frozen/model fingerprint不变；cleanup容器/卷/网络均0、临时目录移除。

因此批准在同一冻结source/workload/image和资源stop gates下进入2-lane；这不预判2路输出、实际CUDA overlap或吞吐收益，也不自动批准4-lane。2路通过后仍需独立复算所有相同项并确认跨lane CUDA事件区间真实重叠。

离线指标仍有两个失败场景/分组统计限制：resource-stop/transport异常会写 `response_completed_monotonic_ns`，所以这类行可能被 `response_completed_count` 当成响应完成；stage在resource stop下会fail-closed，复算失败轮时须以HTTP `status_code` 是否存在区分真实response与仅结束等待。另外，`duration_stats_by_family_round`受组外层循环顺序影响，不能当成各family按round独立测得的吞吐；已用raw timestamps计算上面的连续phase吞吐，并保留每组每轮窗口。两点不影响本轮全部240条HTTP成功的reference结论。

## 本轮独立检查结果

- Cached image 元数据包版本及五个关键依赖源码 SHA 与当前锁定依赖一致；Qwen3 配置 `rope_scaling=null`，cached Qwen3 `use_cache=None` 默认不会创建 `DynamicCache`。完整、可复核的只读证据见[缓存运行时源码复核](cached-runtime-source-review.md)。这支持 scoped adapter 的前提，但不证明 GPU 运行时数值行为。
- 当前 frozen copy 的 embedding 整体 SentenceTransformer forward 位于每 lane CUDA stream，并在执行前等待原 stream 的 ready event；embedding 的 `use_cache` guard 仅允许原始省略/None 或 False、拒绝 past KV；reranker 维持 `use_cache=False`，并在 tokenize、拼接固定前后缀和 pad 期间使用 tokenizer lock。模型固定 eval/device guard、参数和 buffer 前后状态快照、warmup 与 admission drain 后 close 均可在副本源码中找到。尚需通过真实服务启动/输出验证这些 guard 的运行时路径。
- 独立 CPU合同文件 [test_experimental_admission_contract.py](test_experimental_admission_contract.py) 结果：`uv run --no-sync pytest -q .../test_experimental_admission_contract.py`，**16 passed**。覆盖 lanes 1/2/4 的 waiting capacity、queue-full、deadline timeout、queued cancellation、active caller cancellation 保持槽、close 拒绝 waiters/drain active；额外断言 active_disconnect 事件 request ID/operation kind 关联及 lane drain。它只证明确定性 Admission 状态机，不证明 HTTP 断连探测、模型并行安全或 GPU overlap。
- 独立 HTTP-stage 纯函数合同 [test_model_http_stage_analysis.py](test_model_http_stage_analysis.py) 结果：**6 passed**、Ruff通过。覆盖 CUDA interval sweep 峰值按同时活动的不同 lane 计算（而非整个时间线的 lane 并集）、相接区间不重叠、embedding cosine/L2 与 rerank 排名指标、reference identity 仅剔除 lane_count，以及 bool/string/NaN/Inf/超大整数/极端有限浮点输入的 fail-closed 数值比较。它不启动 HTTP client，也不证明真实服务事件或 CUDA 执行。
- 当前未审真实 1/2/4 GPU forward 区间、固定输入输出误差、GPU/RAM 阈值和 run 清理，因此实际模型并行结果仍是**待测**；Admission CPU合同通过不提升为模型或生产验收。

### 2-lane 实测与独立对照

Run `20261012T124736Z-eb9321` 与API1使用相同 `151fbd6e…a994f` experiment source、formal source、workload SHA、缓存image与模型身份。240/240请求HTTP 200且ID唯一，0 errors；96/48/96 family count、response/server status correlation、事件完整、24 mixed waves均通过。独立对比文件[API 1/2逐请求事件与分组表](independent-comparison.md)由冻结的client/server raw timestamps计算，queue=`admission_enqueued→worker_dequeued`、execution=`operation_enter→operation_exit`、server E2E=`request_received→request_completed`各自直接计时，不以端到端减队列反推执行时间。表中分别给出统计中位数、nearest-rank P50/P95/max，并按family total及group-round分开；吞吐用连续family phase窗口。API2 client E2E统计中位数/nearest-rank P50/P95/max为：embedding 385.51/384.43/755.01/763.63 ms，rerank 351.22/347.82/706.33/743.78 ms，mixed 395.50/393.99/754.59/802.36 ms。

对API1 reference逐项比较，六组输出输入数与embedding维度1024正确；embedding norm与API1同范围，最小cosine 0.9999999999999998，最大L2和max-abs均0；rerank最大分数误差0、96/96排序与Top-3一致。服务端CUDA区间raw复算648个forward end，lane 0和1同时活跃峰值2、跨lane interval pair 415、同lane overlap 0。Embedding与reranker跨类型overlap按独立request pair为9（底层forward-interval pair数17）；这是同GPU两lane stream事件时间区间重叠，不等价于kernel profiler或长期容量承诺。API2 GPU free最小3798 MiB、host free最小4928 MiB，未触发1024/3072 MiB stop线；GPU util max100%、model-node CPU max196.55%、容器RSS max约3018 MiB。host/GPU采样165行、间隔median/max 563/719ms；Docker有效容器采样23行、median/max 4031/4781ms（名义2s）。结束formal/frozen/model fingerprint unchanged，cleanup资源0。

2-lane boundary run `20261012T125116Z-ee4f61` 也通过：15个HTTP请求，queue-full/timeout/queued-cancel、CPU active disconnect、GPU active disconnect均通过；GPU disconnect后12个forward end都早于operation exit/lane drain，随后新请求有效；formal/frozen/model状态不变，cleanup资源0。见该run的 `boundary-summary.json` 和 `model-events.jsonl`。

**允许已验证的2-lane实验结论；4-lane暂缓且未启动。**API2运行GPU free最低3798 MiB，比硬停止线1024 MiB仅高2774 MiB；相较API1最低4777 MiB，2路运行最低点少约979 MiB。GPU利用率两档均达到100%，宿主free余量为1856 MiB，容器采样间隔最长约4.8秒，且没有足以预估4路峰值activation memory的证据。继续4路的风险判断不能假装线性预测；但现有余量/采样不足以在OOM前证明4路有安全预算，因此本轮停止在2路。**不声称4路OOM、不可运行或已被测；只记为安全原因未尝试。**

## MCP 阶段静态复核与实际结果

对 [mcp_stage.py](mcp_stage.py) 及其冻结 helper 做了只读检查。shared synthetic corpus 写入隔离 runtime root，以 `blocks-split` 构建且要求至少 8 个 chunk；查询结果逐条核 shared library/version/target/marker。managed fixture 通过上传作业完成后读取 active version，查询逐条核 file/version/marker；两次竞争 ingest 后再发 readback query，避免把上传前开始的 query当成新版本读取。阶段通过两个独立 reader 个人 credential 建立独立 MCP stateful session；helper 执行`initialize`、`MCP-Session-Id`、`notifications/initialized`、`tools/list`，对有 grant 的 A 与无 grant 的 B 验证 shared/managed访问。未授权结果须HTTP成功、工具错误且 evidence 为空。

初轮 `20261012T125406Z-4a9f0c` 在35条期望调用中记录31条后，后续上传资源授权失败，outcome失败；日志证实上传新资源后未先同步 `/api/admin/resources`，授权端返回404。该run是失败partial，31条行记录通过不能替代整阶段通过，也不纳入性能分母。修复仅在实验程序加入资源目录GET同步及失败状态/响应诊断，formal业务源未改。

MCP1 run `20261012T125703Z-2d27c2` 已独立复算：35/35 tool-call rows均HTTP 200且逐项合同通过；其中性能查询24/24（3轮×每轮8条，shared/managed各12条），授权拒绝2、warmup2、stale-version拒绝1、query-ingest并发前置查询4、ingest后readback2。共享与受管初始fixture均18 chunks；两次competition ingest各18 chunks，readback按file/version/marker核验通过。每轮性能阶段时长2.872/2.650/2.531s；按客户端单调用elapsed原始值统计的MCP E2E median/nearest-rank P50/P95/max分别为shared n=12: 1060.71/1034.10/1587.86/1587.86ms，managed n=12: 1074.73/1028.74/1396.52/1396.52ms。该端到端时延含MCP/auth/retrieval/model全链路，不能解释为纯模型时间。

按每个tool-call raw metadata，shared性能查询12次均`dense_returned=10`、`bm25_returned=10`、`fused_candidates=12`、`reranker_used=true`；managed性能查询12次同样dense/BM25/fused计数，但`reranker_used=false`。两条post-ingest managed readback也是不重排（`fused_candidates=13`）。冻结源码路径解释与观测一致：shared通过`_legacy_service`注入`shared_reranker`；managed `/api/admin/resources`授权后路由`_search`→`FileManagementService.search`→`RetrievalService`，该构造未注入reranker。故本轮能声称shared MCP路径进行了模型重排；managed MCP路径只做dense/BM25召回和RRF融合，不能声称重排。这是当前接线行为记录，未修改检索参数或业务实现；独立模型API reranker测试不改变这一结论。

MCP1关联model events共1106条：62个model API HTTP arrival/completion，55个admission/operation；operation按`embedding:query=32`、`embedding:document=8`、`rerank=15`。对应forward_end为embedding query32、document8、reranker90。lane=1时CUDA event活跃峰值lane数1；没有GPU多lane overlap。该事件数包括warmup、managed/shared查询、索引和ingest，不能把每个MCP调用简化成一次forward。model trace与MCP client tool-call request id不是同一命名空间，报告不虚构逐调用ID映射；MCP层仍用完整tool-call elapsed，模型层使用服务端自己的queue/operation/CUDA直接事件。

MCP2 run `20261012T125923Z-b93726` 在协议请求流量前失败：`ModelProviderError`, safe code `model_identity_invalid`；无`mcp-summary.json`、无MCP tool rows。formal/frozen/model state unchanged，cleanup容器/卷/网络0、临时root已移除。此为MCP2启动失败，既不代表MCP2链路正确也不代表行为失败，未纳入性能比较；需要新的同源MCP2有效run才能做1/2端到端比较。MCP阶段cold口径仅`container process→ready`且包含自动warmup；OS cache已热，cold HTTP推理未测，不称磁盘cold。

## 源码证据

- [admission.py](../../../src/wenjin/model_api/admission.py)：`ModelAdmission.__init__` 创建单个 `_worker`（48行）；waiting 队列是 FIFO（71行）；worker 设置 started 后再调用 `run_in_threadpool`（126–130行）；shutdown 会拒绝 queued 并等待 worker（149行后）。每个服务进程单 worker 是当前配置/实现事实。
- [server.py](../../../src/wenjin/model_api/server.py)：runtime 在一个 `RLock` 下执行 embedding、rerank 和 close（39–71行）；服务 lifespan 装配单个 `ModelAdmission`（225行附近）；HTTP handler 通过监视 `request.is_disconnected()` 取消 submit 等待（288行后）。
- [file_management/service.py](../../../src/wenjin/file_management/service.py)：管理进程有自己的 `_model_lock`（132行），受管查询期间使用它（962行）。这是与模型 API 进程 `inference_lock` 不同的锁，不要将它解释成同一把跨进程互斥锁。
- [模型准入开发记录](../2026-10-12-model-admission/development-log.md)：此前 GPU run 的 query+ingest 客户端区间重叠 0.609 秒，但没有服务端 queue/active 时间，因此明确未证明 GPU 模型竞争/排队。此前基线 also observed upstream model 503，但没有 1/2/4 分档的服务端 queue timeline。

**事件要求：**至少把 admission enqueue/dequeue、admission worker start、`inference_lock` wait/acquire/release、真实 backend forward start/end、HTTP completion 和 queue-full/timeout/cancel/disconnect 直接记录并按请求 ID 关联。RLock acquisition 或 `ModelAdmission._run` 中的 `started` 事件都不是实际 forward start。所有 latency 应分为 queue wait、lock wait、forward duration、端到端；应从原始事件独立重建区间 overlap。

## 正式验收尚未通过

目前已完成 runner 和 direct model stage 静态复核，仍未审最终 frozen run 产物，也没有真实模型 run 产物。因此暂不能对 GPU 实测重叠、模型输出误差、资源阈值或清理作通过结论。初审列出的 runner 启动阻断已在当前版本关闭：

- state/env SHA 与受限路径检查在读取 bearer token 和 compose 前执行；冻结/final source hash 与 model state fingerprint 进入最终 pass；采样错误、未停止采样线程、Docker资源残留或未知值、graceful stop/cleanup失败均使该轮不通过；Docker采样命令有上限时限；启动后校验模型容器唯一性和实际 image ID；宿主唯一 GPU身份冻结并通过 `CUDA_VISIBLE_DEVICES=0` 固定可见设备。
- direct HTTP 输出和 reference 的类型/有限数转换、超大整数转换和 extreme finite 运算已 fail-closed。独立合同覆盖 `10**10000`、`1e308`、bool/string/NaN/Inf，验证没有异常逸出且数值比较拒收。

`boundary_http_stage.py` 已补齐并经静态复核，Admission CPU合同覆盖其核心准入状态机；但真实 HTTP queue-full/timeout/queued-cancel/active-disconnect 阶段尚无成功实测。真实断连仍须证明 forward CUDA end 晚于服务端识别 disconnect，并等 drain 后才放行后续请求。

生成 freeze 后仍须逐项核对[独立验收计划](independent-acceptance-plan.md)，尤其是：

1. 1/2/4 是否在同一个 model container、同一权重 hash、同一服务进程/实例中运行；所有 run 是否冻结 harness/config/srcdiff/image/model identity。
2. 1/2/4 请求是否由服务端 arrival/queue/worker/lock/forward 事件证实；区分排队/锁等待/真实 forward 重叠。
3. 相同固定 query/document/rerank 输入是否有重复轮、多长度和多候选；embedding 的维度/norm/cosine/L2 误差以及 reranker score/rank 误差是否能由原始结果复算。
4. CPU Admission 合同通过不代表 HTTP 或模型行为；真实 HTTP queue full、等待 timeout、排队取消、active disconnect 仍需由 root 在真实服务档验证。active 同步执行及 CUDA event 应由 worker 持有至真实结束。对 cancel 返回或客户端 future 取消本身不能当作 forward 已停。
5. 资源阈值是否先冻结、采样时钟是否能对齐事件；硬阈值停止是否确实执行；清理后容器/卷/网络/临时目录为零。
6. 仅在 direct HTTP 阶段稳定后，审查状态化 MCP 多切片、授权/版本隔离及 query/ingest 竞争；模型层数据不替代端到端 MCP 结论。

已完成的是**模型调用边界和 cached image 依赖源码的只读核查**，不是 adapter 测试或模型服务验收。包版本、依赖源码 hash、encode/to/eval/preprocess、tokenizer、Qwen3 cache/RoPE 结论见[源码复核证据](cached-runtime-source-review.md)。此前 2026-10-12 并发基线、GPU targeted run 与 CPU admission 合同只作历史背景，各自范围不替代本轮真实 1/2/4 多路实验。

## 两次真实服务启动失败及缓存接口复核（2026-10-12）

run `20261012T121422Z-b42d0b` 在性能请求前因缓存 Starlette 不支持实验入口注册方式退出；run `20261012T122345Z-5d8d9d` 在性能请求前因 adapter 对不存在的 `model._parallel_process` 属性作严格校验而退出。后者 `model-events.jsonl` 为空；两个 run 均非 1 路性能样本，均不得纳入 throughput/latency/error denominator，也不允许据此进入 2 路。root 记录两轮均已停止并清理；122345 的 cleanup 为容器/卷/网络均 0、临时目录已删除，formal/frozen source unchanged。

为定位第二轮 guard，仅在缓存模型镜像中执行无网络、无GPU、无权重挂载的只读 Python introspection：镜像 `sha256:c9d4e517…fdca3e` 的 SentenceTransformers 是 5.7.0，`SentenceTransformer` 本身没有 `parallel_process` 属性；外层 LlamaIndex `HuggingFaceEmbedding`（0.8.0）则具有 PrivateAttr `_parallel_process`、`_target_devices`，init保存 `parallel_process` 参数，`_embed_with_retry` 仅在该 flag 为 True 时创建 multi-process pool，否则调用底层 ST `encode()`。因此 run `122345` 的 frozen adapter 把 flag 错读自 `embedder._model`（内层 ST），缺属性按默认 True 拒绝，属于实验 guard 误判，不是模型结果。已通知 root/harness：正确对象是 outer `runtime.embedder._parallel_process`，并需在每次调用前保持 False；底层 ST `encode()` 的 `pool`/多设备路径可进一步做 fail-closed guard，固定实际 device。当前工作树修正尚待新 freeze 复核；不得把普通 startup 修复解释为模型安全或性能通过。

本次尚未完成对修正后 freeze 的复核。只有单路 readiness 成功、模型 identity 与权重冻结一致、固定负载原始 request/response/event 和资源采样完整、所有 fail gate 通过后，才能产生单路 reference；再以该 reference 评估 2 路。两个失败启动记录保留为失败样本，不覆盖。



## 第三轮单路结果独立复算（2026-10-12，拒收为性能样本）

Run `20261012T123014Z-42bd9d`（phase=model、lanes=1、4个HTTP client、3轮、6组×8请求）冻结身份可复核：241项 frozen file hash 全匹配；frozen experiment source tree hash 与 state/freeze 的 `552293ec…56fd2c` 相符，formal source tree hash 与 `c0aa7dfc…f5a674` 相符；prepare 时 state/env hash 已和 freeze 相符。模型及 backend/Qdrant image digest、单 GPU身份冻结。结束时 outcome 记录 formal/frozen unchanged，cleanup 记录容器/卷/网络均0且临时目录已删除。

服务 readiness、模型identity和adapter启动成功；outer LlamaIndex `_parallel_process=false`、embedding/reranker 均 `cuda:0`，参数/buffer before fingerprint已记录。但原始 client log 144/144状态为 HTTP 400 `invalid_request`，6组各24条，ID全唯一且 response/server request ID全匹配。直接比对 frozen request builder 与 frozen服务端可定位原因：stage 把 MCP `2025-11-25` 写入REST payload；该服务要求 `wenjin-model-api/1`，在协议校验处拒绝请求。server trace计150个 `request_received` + 150个 `request_completed`（6个health/identity/warmup为200，144个测量为400）；没有 queue/worker/operation/forward 事件，`forward_event_count=0`、CUDA peak lanes=0、reference output为空，summary `failure_ratio=1.0`、`stage_pass=false`。所以混合 HTTP overlap仅是无效请求的handler重叠，不是模型竞争；测得HTTP错误响应 p50=10.829ms、p95=14.777ms、max=223.410ms 仅供故障定位，不作吞吐/端到端推理指标。**不得将此run作为单路reference，不允许据此进入2路。**

资源原始采样：GPU/宿主103条，实测采样间隔median=562ms、max=594ms；free GPU最低6584MiB（总12282MiB）、util最高23%；host free RAM最低4320MiB。Docker 14次，实际采样间隔median=4687ms、max=4766ms（名义2s），13次含容器样本；model-node采样最大约2730MiB、CPU最高115.02%。effective stop阈值GPU free 1024MiB / host free RAM 3072MiB均未触发；这些资源点不覆盖有效推理负载峰值。摘要与原始请求/事件状态一致。下一轮须修正API protocol并重新生成独立freeze；保留本轮失败结果，不回填向量或篡改状态。

## 主Agent汇总已收到的独立验收意见（非新增子Agent独立复核）

以上早期startup/schema拒收章节保留历史位置，不代表最终状态。独立Luna Agent已在消息中复算最终API1 `124501Z-fe800f` 与API2 `124736Z-eb9321`：240/240/档，数值逐值一致、维度1024/norm/排序正确，correlation/events齐，CUDA peak1/2，同lane无overlap；模型张量/formal/frozen unchanged、cleanup0、资源门未触发，允许进入2。独立复核boundary `124331Z-e77826` 与 `125116Z-ee4f61` 真实HTTP取消通过：queued_cancel退出，active GPU disconnect后12 forward结束，operation_exit再lane_drained，fresh成功。曾按原始MCP1 `125703Z-2d27c2` 复核35工具调用、18切片、授权和版本/证据；定位managed不传reranker以及正式client active==1边界。隔离MCP client lane绑定CPU合同3项通过。

子Agent随后额度限制，最终同client源码MCP1 `130402Z-ddb5c7` 和MCP2 `130745Z-6ff9dd` **没有收到子Agent最终独立签收**。主Agent完成原始复算、hash/resource/cleanup/证据审核，结果见 [final-evidence.json](final-evidence.json) 和 [final-report.md](final-report.md)，MCP2 independent status明确未完成。不得把此汇总写成子Agent复核过最终MCP2。四路安全预算未证实，独立意见为不强行启动，最终记未验证。
