# 单模型多路并发实验独立验收计划

日期：2026-10-12  
范围：真实单份模型服务的 1/2/4 路请求准入实验。此文件是预先约定的只读验收口径；不代表已启动模型、发起流量或完成实验。

## 当前安全结论

当前实现的 `ModelAdmission` 每个 API 进程只有一个 FIFO worker；`ModelAPIRuntime` 的 `inference_lock` 另以 `RLock` 包住 embedding 和 rerank 模型调用。默认配置因此是单路执行。可以通过隔离配置或实验 overlay 验证 1/2/4 个 HTTP 请求同时在同一模型服务中等待、排队和被分派；若只增加 admission worker 而保留 runtime 锁，实际模型 forward 仍应串行。

生产源码仍保留 `inference_lock`；普通多 admission lane 不等于 GPU 并行执行。锁保护共享 embedding/reranker runtime 的调用与关闭生命周期。只读检查 cached image 的依赖源码和固定模型配置后，已批准在私有 benchmark copy 中使用受限 adapter 实测真实 forward overlap：固定 device/eval 状态并对 `.to()`/`.eval()` 做严格 guard；embedding preprocess 与 reranker tokenizer/pad 串行；权重只读；embedding cache 显式关闭且先证明 1 lane 等价；每 lane 独立 CUDA stream；warm load/close/drain 保持保护；每轮对参数和 buffers 做前后指纹。此授权只覆盖 isolated 实验，不代表生产线程安全或允许删除应用锁。模型节点保持一个服务实例和一份权重。

## 必须冻结的实验身份

每个 1/2/4 lane 档位及重复轮次分别保存不可变 run 目录，启动流量前至少冻结：

- harness 文件本体、依赖/入口和 SHA-256；实验用配置、Compose overlay、代码 revision，以及相对于同一基线源码的完整 `srcdiff`。
- 容器镜像 digest、容器 ID、模型服务实例 ID、checkpoint/Tokenizer 文件 hash、模型对外 identity、GPU 名称与驱动/运行时身份；证明全过程只有一个模型容器和一份已加载权重。
- 请求清单、固定输入及每项文本 hash、候选文档内容/hash/数量、重复轮次顺序、预热规则、负载发生器版本和随机种子（若有）。
- 每轮开始前、结束后及清理后的容器/卷/网络/临时目录清单与 ID。明确不得指向既有索引、资料、服务或凭据；运行产物必须脱敏，不能保存 token、密码、Authorization 或完整 session ID。

流量配置先以 1、2、4 路并发请求为档，固定每档相同工作量。每项至少有短输入、长输入、候选数量低/中/高三类 query embedding、document embedding 和 rerank 请求；若一种 operation 不适用某个维度，应记录原因。每个组合做多轮、轮间静置到队列清空；另设短预热且不混入测量。请求要有安全的匿名 request ID、operation ID 和 monotonic 时间戳。

## 服务端事件与重叠判据

必须从模型服务本身写事件，而非根据客户端发送区间推断：`request_received`、`admission_enqueued`、`worker_dequeued`、`worker_start`、实验 preprocess/tokenizer lock wait/acquire/release、真实 embedding/rerank backend 的 `forward_dispatch` 以及每 lane CUDA `forward_end` 时间、`request_completed`；另记排队取消、active 断连、超时、队列满和 shutdown 拒绝。所有事件带同一个 request/operation ID、lane/worker ID、input kind、queue depth、active 数、模型 identity 与 monotonic/UTC 时间。实验 adapter 在样本期间绕过 runtime `inference_lock`，这是被批准的隔离变更；该锁仍保护加载、安装 adapter 和 drain 后关闭。不能要求或伪造每次请求的 runtime lock acquisition 事件，也不能将 `forward_dispatch` 当作 GPU 开始。真实 CUDA 起止以共享 origin 后每 lane 的 CUDA event 计时为准。

统计上必须区分：

- **请求重叠**：客户端并发发送且服务端在途请求数确实达到 1/2/4。
- **队列并发**：服务端事件证明请求进入同一个模型实例，存在 active 与 queued 项，且队列峰值如实记录。
- **准入 lane 并发**：多个 worker 同时被分派；这不等于模型调用并行。
- **模型执行重叠**：backend forward 的区间两两交叠。只有来自实际 forward 边界的事件才能支持该结论。若锁保留，预期同实例 execution overlap 为零，锁等待会显露为单独阶段。

队列延迟拆出 `worker_dequeued - admission_enqueued` 与 `forward_dispatch - worker_dequeued`；分别报告 embedding preprocess/reranker tokenizer 锁等待、host forward dispatch、共享 CUDA origin 到 CUDA start 的偏移、CUDA forward duration 和端到端时长。不要将 CPU dispatch 区间当成 GPU forward；从各 lane 的共享 origin 相对 CUDA event 起止重建 device overlap。

## 正确性与误差检查

- 对 embedding 输出校验响应数、维度、有限数；逐向量计算 L2 norm、零向量率、最小/最大 norm，并按 query/document 与短长输入分组。维度须对应被冻结的模型 identity。与单路同输入基准逐项计算 cosine similarity、L2/max-abs 误差和方向偏移；量化/浮点差异阈值在运行前写进 config，不得看完结果再挑阈值。
- 对 rerank 输出校验候选数量、每项分数有限且顺序与输入 ID 一致；和单路固定版本结果比较 score MAE/max-abs、Spearman/Kendall 排名一致性及 Top-k 翻转。用真实 scorer 返回而非合成值；若并行档输出无法区分文档身份，该轮无效。
- 每个超载、超时、取消或断连都计入请求分母和结果分类。错误必须不带部分向量/分数成功内容；检查后续请求仍能得到正确输出。

## 队列与取消压力边界

在独立受控场景验证：等待位满时稳定返回 queue-full；队列等待 deadline 返回 queue-timeout；排队中的取消/断连能移出队列且不占槽；active 请求断连后实际同步 operation 继续持有 active 槽，直到线程及其 CUDA event 完成才释放；随后的请求在此之前不能误占同一槽；完成后队列恢复。当前独立 CPU 合同测试已覆盖准入状态机与 active_disconnect 的 request/op 事件关联；它不证明 HTTP disconnect 检测、模型推理或 CUDA slot 生命周期。真实模型档仍需观察真实 HTTP 客户端断开及服务端事件，不用高负载或长输入制造不受控卡死。

每档设置资源停止阈值并冻结在配置中：GPU 显存余量、GPU 利用率、模型容器内存/CPU、宿主可用内存、队列深度、请求时限和总运行时长。任一硬阈值触发即停本档，记录停止原因；不得通过重试扩大压力。资源以容器与 GPU 采样记录，并注明采样间隔、最大采样值非瞬时峰值。

## 稳定后才进行的 MCP 端到端阶段

模型 HTTP 阶段通过后，另起冻结 run 验证 MCP `2025-11-25` 状态化 initialize/initialized 和会话能力协商。使用多切片合成资料、隔离 Qdrant/账号/ACL，授权与未授权身份、shared 与 managed 两链路、固定 release/file/version；每个 query 的证据 locator/hash、target/library/file/version、正文标记必须与输入 fixture 对应。并发 query 与 ingest 分开测同一时段竞争和顺序对照，捕获 model API queue/execution 事件以及 MCP/管理桥关联 ID。替换、撤权、重授权、删除按预先定义的版本快照语义检查，任何错误都必须零证据。MCP 阶段不能替代 direct model API 层测量。

## 独立复算与结论分级

验收者从原始事件和逐请求输出重算每档成功数、错误类别、吞吐、p50/p95/p99（注明样本数与算法）、队列长度/等待分布、lock wait、forward duration、区间 overlap、向量误差、rerank 排名变化和资源统计。与脚本摘要逐字段比对，不只审阅总表。失败样本保留，不能覆盖为新轮成功。

结论只能分为：脚本合同/可控阻塞验证、真实单模型 HTTP 请求与队列验证、真实模型 forward overlap 验证、MCP 隔离链路验证、部署/生产验收。实验 adapter 即使被接受，也只支持这份冻结镜像/权重/配置及资源阈值下的 scoped overlap 观察；不能推成服务默认配置、生产 thread-safety 或普遍容量。若没有 backend/CUDA 区间事件，不声称 GPU execution 重叠；若未跨真实部署环境运行，不声称生产容量。

## 已完成静态核查

只读核查了 `src/wenjin/model_api/admission.py`、`src/wenjin/model_api/server.py`、`src/wenjin/retrieval/service.py`、`src/wenjin/file_management/service.py`、`src/wenjin/management_http/app.py`、对应模型准入记录和首轮并发报告。正式模型 API 默认 admission 为单 worker，`ModelAPIRuntime` 的 `RLock` 包住 embedding/rerank 和 runtime close。队列满/等待超时为 503；排队任务取消会移除，active 同步模型操作由 worker 持有直至线程结束。以上是源码行为结论。缓存模型镜像的包版本、五个源文件 SHA、Qwen3 rope/cache限制和adapter审核结论见[缓存运行时源码复核](cached-runtime-source-review.md)。本节为源码核查；真实boundary与1-lane reference运行状态见“执行状态补记”。

实验 overlay/source diff、事件字段、采样与复算程序已审；1-lane boundary与reference已完成独立复算。当前只允许进入2-lane，其实际forward overlap、数值、资源和清理记录仍待验收；尚未通过2/4-lane性能验收或MCP多切片阶段。

## 执行状态补记（2026-10-12）

隔离 HTTP boundary 轮 `20261012T124331Z-e77826` 已由主 Agent 执行并经独立原始事件复算：queue-full、queue-timeout、queued-cancel、CPU active disconnect、真实 GPU active disconnect 均通过；GPU disconnect 后 CUDA forward 全部结束，才出现 operation exit 与 lane drain，断连后请求有效，正式源码/冻结快照/模型状态保持不变且资源清理为0。此项仅完成队列/取消/槽生命周期边界，不构成 2/4-lane 性能或生产线程安全证据。记录见[独立验收报告](independent-acceptance-report.md)。

三类 workload 已冻结为纯 embedding、纯 rerank、mixed 2+2 三个独立阶段。1-lane最终性能 run `20261012T124501Z-fe800f` 的240条原始请求、reference数值、服务端事件、CUDA interval、资源阈值、freeze hash/state与cleanup已独立复算通过。2-lane run `20261012T124736Z-eb9321` 的同样检查通过并复现跨lane CUDA interval overlap；boundary2 `20261012T125116Z-ee4f61` 的真实HTTP queue/cancel/disconnect与forward持槽检查也通过。**4-lane本轮不启动，因资源余量与采样分辨率不足以在OOM前证明安全预算；这是未测试，不代表不可承载。**MCP多切片阶段按单/双lane后续另行验收。

资源实际记录需按冻结阈值解释：GPU free 1024 MiB、host free 3072 MiB、连续容器重启2为本run硬stop；GPU利用率和model-node CPU只有观测没有硬stop，不能误报成已配置门槛。1-lane中GPU利用率达到100%、容器CPU达到248%且Docker采样间隔长于名义2秒，已在报告中披露；2-lane如有资源stop信号必须立即终止该档，且不得自动进4-lane。
