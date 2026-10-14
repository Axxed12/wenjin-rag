# 模型多路并发实验（2026-10-12）

## 范围与当前状态

主Agent统一审核、运行和汇总；GPT-6 Luna high分别开发实验程序和独立验收。请求由程序产生。仅写本轮artifact、code.md当前结论和开发索引；不改正式源码/配置/默认值，不引入缓存、Redis、动态批处理或检索调参，不运行冻结评测及无关完整测试。

已读AGENTS.md、PROJECT.md、code.md、最新[模型准入迭代](../2026-10-12-model-admission/development-log.md)及[首轮并发报告](../2026-10-12-concurrency-baseline/final-report.md)。这些历史实测仅供定位，不计为本轮结果。当前正式源码只有单worker ModelAdmission和全程runtime RLock；max_waiting/max_wait_seconds不是同时推理配置。

## 实验设计与停止条件

- 第一阶段真实模型HTTP：固定客户端在途4，分别query embedding、多个候选rerank、两者混合；短/长输入及不同候选数的输入清单固定。各1/2/4服务推理名额在预热后执行3轮相同负载，冷启动单列。两路稳定通过才启动四路。
- 不通过多worker和全程锁制造假并发。独立实验入口需保护lazy load/close/tokenizer等可变状态，单份模型共享只读forward；审查未能安全拆分时停止真实多路并明确原因。
- 服务端直接记录入队、派发、准备锁、forward、完成、取消事件与请求关联；CUDA事件和CPU事件分别解释。排队、执行、端到端分别统计，不相减猜测未测项。
- 隔离随机Docker项目/临时资料/账号/Qdrant/数据；全部端口仅127.0.0.1，现有权重只读，仅一份Embedding和一份Reranker。每轮启动前冻结完整实验程序、源码/差异、配置、镜像与模型身份。
- 启动前GPU空闲至少8GiB、宿主可用内存至少6GiB；运行时GPU空闲<1024MiB或宿主可用<3072MiB、OOM/容器OOMKilled、数值契约失败或>=5%非预期失败即停止加压并保存证据。队列满/超时专用边界探针不混入性能失败率。
- 第二阶段仅在模型API稳定后执行共享+受管、query+ingest的真实stateful MCP2025-11-25，多切片fixture及个人凭据；验证资料/版本/授权/实际召回重排数量。完整链路收益不可自动归因GPU并行。
- 边界检查覆盖队列满、超时、排队取消及active断连；底层工作结束前必须持槽，断连不等于GPU即时停止。

## 本轮资源核查

[preflight.json](preflight.json)实时检查：4070 SUPER显存12282MiB、已用1799MiB，宿主可用RAM9287MiB，Docker约15.2GiB/12CPU，7个既有运行容器保留。缓存模型/后端镜像仅作为依赖基底，实际挂载本轮冻结源码。后续结果和清理证据持续追加，不将准备或静态检查标为实测通过。

## 教学最小片段

`await asyncio.gather(send(a), send(b))`接收两个请求协程，返回对应顺序的结果列表；它只保证客户端同时等待。例如两个HTTP请求都已发送，服务端仍可排队依次执行。是否真实同时推理必须另看服务端forward开始/完成事件，本轮据此划分客户端在途、worker占槽和实际执行。

## 下一步

审查独立runtime adapter的共享可变状态，完成程序离线必要合同后冻结并启动单路；本文件将追加实际命令、失败与各阶段验收结果。

## 隔离adapter审核决定（尚非实测）

正式API没有同时推理配置，不能只增加worker而保留全程runtime锁后声称多路。主Agent批准仅在artifact独立实验入口中细化：单线程warmup后固定eval/device/dtype，对实验SentenceTransformer实例的to/eval使用严格状态校验保护；preprocess和reranker tokenize/pad仍串行；单份weights上的forward用每lane CUDA stream及局部张量，真实CUDA完成后才释放名额；关闭保留stop/drain顺序。模型默认RoPE、dropout0及非共享KV cache为必要条件，出现未支持配置立即拒绝实验。

独立无网/无GPU/无权重挂载的缓存镜像依赖检查：torch2.11.0+cu128、Transformers4.57.6、SentenceTransformers5.7.0、LlamaIndex HF0.8.0，关键依赖文件与主环境一致（完整检查证据由验收Agent保存）。encode每次to/eval会写共享状态，tokenizer也不能无锁，所以直接移除原runtime锁不作为本实验方案。Embedding显式禁用局部cache时需要同参数单路原实现输出对照；所有组使用同一adapter变项。

以[business-files-before.json](business-files-before.json)冻结116个src资源与正式配置/lock文件的内容hash，结束时逐文件复核。实验限定条件与状态/输出检查不是生产共享模型线程安全保证，设备区间重叠也不宣称已经测得底层kernel并行。

## 独立验收合同补充（CPU-only）

新增 [test_model_http_stage_analysis.py](test_model_http_stage_analysis.py)，只导入HTTP阶段纯分析函数、不建立HTTP连接或启动GPU。与既有Admission CPU合同合并运行：`uv run --no-sync pytest -q artifacts/iterations/2026-10-12-model-multilane-benchmark/test_experimental_admission_contract.py artifacts/iterations/2026-10-12-model-multilane-benchmark/test_model_http_stage_analysis.py`，20 passed；对应Ruff检查通过。覆盖Admission的等待/取消/关闭合同，以及CUDA事件区间峰值、相邻事件端点、identity绑定和向量/排序指标。该检查不替代HTTP/模型/GPU验收。

独立静态复核仍阻止启动：experiment runner未将state和路径与freeze绑定、source/frozen内容复核与清理审计未进pass门槛、资源采样线程/长命令退出未验证、实际model-node容器数和image digest未核对；HTTP结果和reference数值转换遇到类型异常会中断而非记录失败。修复并重建冻结产物前不执行GPU阶段。

## 最新静态复核（2026-10-12）

root修复上述runner边界：state/env hash和路径先验校验、正式源码与冻结文件最终pass、资源采样失败与超时fail-closed、容器清理未知/残留拒收、模型容器唯一及image digest校验，并冻结单GPU身份和容器可见GPU。direct model数值输入现对布尔/字符串/非有限数/超大整数/极端浮点做fail-closed检查；identity binding、事件peak sweep和CPU边界测试已复核。最终离线命令结果：Admission及HTTP纯函数合同 **22 passed**，Ruff通过，7个实验/runner Python文件AST解析通过。此结论仅允许进入重新prepare和Compose静态检查，不表示已经启动GPU或验证真实推理。

`boundary_http_stage.py` 仍不存在，不能声称真实HTTP queue-full、timeout、排队取消、active断连验收已覆盖；这与direct model性能阶段分开跟踪。此前本日志“静态复核仍阻止启动”段落描述的是已修前版本，现状以本节为准。

## 首次启动失败与修复方向（本轮实测）

run `20261012T121422Z-b42d0b`，隔离project `wenjin-concurrency-ff73fa52`，在任何性能请求发出前model-node反复退出（ExitCode1、OOMKilled=false）。脱敏service-log显示实验入口 `@app.middleware("http")` 与缓存Starlette不兼容（应使用add_middleware），是实验程序缺陷，不作为模型性能或业务缺陷结论。主Agent中断等待，以 `cleanup_failed.py <state>` 验证state hash及严格临时路径后收集日志、停止并删除本project资源，cleanup记录容器/卷/网络均0、临时凭据目录已删除。原冻结快照保留；不得覆盖为修复后成功轮。

独立验收指出六个原有直接API组只有组内并发，缺少Embedding/Reranker真实混合到达；重新冻结前补混合调度。GPU断连边界需证明forward_end晚于服务端识别disconnect，不能只用operation_exit推断GPU仍在执行。runner增加连续重启>=2的停止门槛，避免健康等待空转。以上修复均限定artifact程序/实验副本。

第二次启动run `20261012T122345Z-5d8d9d`也未发出性能请求：中间件修复后，adapter将LlamaIndex的并行进程属性误读自SentenceTransformer对象，严格guard拒绝startup（`experiment_embedding_parallel_process_unsupported`）。runner连续重启门槛按预期停止；正式源码/冻结内容unchanged、cleanup资源0且凭据目录删除。显存资源未触发阈值，不作为4070 SUPER多路能力结论。重新核cached依赖接口后再冻结。

## 单路启动成功但请求协议不匹配（独立复算，2026-10-12）

- 问题：run `20261012T123014Z-42bd9d` 成功ready/identity/adapter启动，但144/144模型请求均HTTP400 `invalid_request`。
- 定位：冻结HTTP stage payload使用MCP `2025-11-25`，而REST模型API正式/隔离server要求 `wenjin-model-api/1`；请求在路由协议校验前失败，未进入Admission/forward。
- 证据：独立读取241项freeze hash全匹配；144个client请求ID与server receipt/completion对应；server只有150 receive/complete事件，无forward事件；summary `failure_ratio=1.0`/`stage_pass=false`/空reference。原始GPU和host资源阈值未触发，cleanup容器/卷/网络0、临时目录已删除。
- 结论：本run不是单路推理基线，不准进入2路；修复protocol后新建冻结run。第三轮资源数据因请求无效不描述有效推理容量。

第三次run `20261012T123014Z-42bd9d` 模型服务启动成功（ready54.26秒），但测试REST payload误用MCP日期：144/144测量请求HTTP400 `invalid_request`，没有Admission/forward事件，不是性能基线。模型API实际协议是 `wenjin-model-api/1`，与保留的MCP `2025-11-25`会话协议分开。独立复核144请求均关联正确，无有效向量reference；所有失败原始结果保留。cleanup容器/卷/网络0，凭据清理、formal/frozen hash不变。之后修测试协议、增加代表输入串行warmup与失败即停止门槛，再fresh freeze。

## HTTP 边界通过与最终单路性能轮启动（独立复核，2026-10-12）

纯 ASGI trace wrapper 已在缓存 backend 镜像、`--network none`、只读挂载实验源码的 mock 合同中通过 health/embedding/rerank。隔离 boundary run `20261012T124331Z-e77826` 独立检查 11 HTTP 请求、144 服务端事件；queue-full、queue-timeout、排队取消、CPU active disconnect、GPU active disconnect 五项均通过。queued cancellation 在原始 trace 有 `queued_cancel`；GPU 请求的 forward dispatch 先于 disconnect，之后12个CUDA `forward_end` 均先于 operation exit 和 lane drain，断连后的新向量请求有效。正式源码、冻结快照和模型状态 unchanged，容器/卷/网络均0且临时目录删除。此为隔离 HTTP/槽生命周期证据，不作为多路性能或生产线程安全结论。

独立静态复核三类性能 workload 的分组、workload SHA和吞吐窗口：每轮纯 embedding 32、纯 rerank 16、mixed 32（8波、每波2 embedding+2 rerank），3轮共240；吞吐由原始client start至response terminal时间窗计算，E2E分位数另报。真实 HTTP transport/resource stop失败时“response completed”字段可能含无status的terminal观测；stop会使该轮fail-closed，后续复算须按是否收到HTTP status区分response count。HTTP input数量也不能等同模型batch，例如12条输入可对应12个独立forward，报告应复算服务端forward event counts。

最终单路 run `20261012T124501Z-fe800f` 启动时的计划记录，随后由下方独立复算结论取代：冻结source与 boundary run 相同，4 client、3 rounds、single lane。

## 最终单路三类 workload 独立复算（2026-10-12）

状态更新：`20261012T124501Z-fe800f` 已完成并通过独立reference gate，允许相同freeze/workload下进入2-lane。原始client请求240条、ID unique 240/240、HTTP 200 240/240、0 errors；pure_embedding/pure_rerank/mixed计数96/48/96；request ID/status correlation、server event completeness、24 mixed waves及跨operation HTTP overlap均通过。六组输出均在3轮repeat，embedding身份维度匹配、norm 0.9999999944–1.0000000621，raw与reference最大L2/max-abs误差均0、最小cosine 0.9999999999999998；rerank候选数正确且scores max-abs 0，故排序、Top-k一致。forward事件实数：query short/long各144，document short/long各36，4候选rerank 96，8候选rerank 192，warmup 16；HTTP输入列表长度不是底层模型batch数。CUDA peak distinct lanes=1、same-lane interval overlap=false。

连续phase raw窗口吞吐：pure embedding 96/16.203s=5.925 req/s，pure rerank 48/7.469s=6.427 req/s，mixed 96/15.922s=6.029 req/s；相应E2E P50/P95/max ms为394.9/857.9/1093.0、363.1/797.3/857.8、417.2/816.1/942.2。`duration_stats_by_family_round`因程序先遍历workload group再遍历round，使同family同轮窗口跨越其他group/轮；不能用其RPS代表独立family-round复验。独立验收已从client raw timestamps重算上述连续phase吞吐，并保留group-round原始窗。

资源：GPU free最小4777 MiB（stop 1024）、host free最小4293 MiB（stop 3072）、restart stop=2均未触发；GPU util最大100%，model-node CPU采样最大248%（配置未设CPU硬停止阈值）。host/GPU样本179条、median/max interval 563/641ms；Docker有效容器样本23条、median/max 4468/4750ms，名义2s。242项frozen files全hash匹配，source/final formal hash与freeze一致，结束后formal/frozen/model state unchanged；cleanup containers/volumes/networks=0、temporary root removed。资源峰值和采样间隔、family-round口径限制与独立2路gate详见[independent-acceptance-report.md](independent-acceptance-report.md)。

## 单路pilot及真实HTTP取消边界

`123702Z-c0ed92`（source988a14…）串行6warmup与144pilot全部成功，结束model tensor fingerprint一致，cleanup0。该调度缺少独立pure reranker比较，因此只作为pipeline/numeric pilot，不是最终三family基线。程序已重新固定240测量请求：pure embedding96、pure rerank48、mixed96，每family三轮；source与程序freeze单独绑定。

`123936Z-a79495` 的5条HTTP边界请求：queue-full/timeout按预期503；queued取消被服务观察成timeout，缺queued_cancel，GPU边界未执行，明确失败。实验BaseHTTPMiddleware可能改写receive/断连语义，改为纯ASGI透传receive，只包装send关联ID，正式代码未改。root缓存mockHTTP检查初次遗漏PYTHONPATH加载旧正式源码缺traceheader失败，随后明确PYTHONPATH=/experiment-source加载实验入口，health/embeddings/rerank均通过；不将前次错误命令写成通过。

`124331Z-e77826`（source151fbd…）11条真实HTTP：队列满/timeout、queued_cancel、active CPU disconnect、active GPU disconnect全部通过独立原始事件验收。GPU请求12条query对应12个forward（HTTPinputs不等于底层批大小），服务端active_disconnect后这些forward继续完成，operation_exit之后才lane_drained，fresh query200。模型tensor状态unchanged，cleanup0且凭据目录删除。这里只证明本机固定实验安全边界，不宣称任意共享模型线程安全。

## 双路模型阶段与边界独立复算（2026-10-12）

API2 run `20261012T124736Z-eb9321` 与API1同source/formal/workload/image freeze。原始240/240请求200、0 errors，reference数值通过：embedding维度1024、norm约0.9999999944–1.0000000621，cosine min 0.9999999999999998、L2/max-abs=0；rerank score max-abs=0，96/96 rank与Top-3一致。按真实forward_end区间独立sweep得到648个测量forward、lane0/1峰值2、415跨lane interval pairs、0同lane重叠；embedding/reranker overlap有9个unique request pairs（17个不同类型forward interval pairs）。API2 phase raw吞吐：pure embedding 7.103 req/s，pure rerank 7.938 req/s，mixed 7.305 req/s；与API1 5.925/6.427/6.029 req/s相比，该固定小样本约提升20%/23%/21%，不是稳定SLO。

API2资源下限：GPU free 3798 MiB（1 GiB stop线以上2774 MiB）、host free 4928 MiB（3 GiB stop线以上1856 MiB），GPU util max100%、model-node CPU max196.55%、RSS约3018MiB；host/GPU sampling165条、median/max interval 563/719ms，Docker有效sample23条、median/max 4031/4781ms。停止阈值未触发，状态hash与cleanup通过，但没有4路activation峰值上界且资源观察分辨率对短峰值粗。安全决策为不启动4路；这是主动停压、不代表4路OOM或不可能承载。独立对照和组/轮延迟表见[independent-comparison.md](independent-comparison.md)。

API2 boundary run `20261012T125116Z-ee4f61` 独立检查15条HTTP、176条server events：queue full/timeout、queued取消、CPU/GPU active disconnect均通过；断开GPU request后12个forward结束均早于operation exit/lane drain，随后的新请求有效，状态hash unchanged、cleanup0。此边界结果同样只限隔离实验生命周期。

## MCP 1-lane独立复算与2-lane启动失败（2026-10-12）

首轮 `20261012T125406Z-4a9f0c` 在35个预期tool calls中只落盘31条后，上传新managed文件的grant请求因未先同步 `/api/admin/resources` 返回404，阶段失败。保留为failed partial，不计作MCP通过或性能轮。实验mcp_stage后续仅增加资源目录同步和授权失败诊断；业务源码未改。

MCP1 `20261012T125703Z-2d27c2` 独立复算：35/35条HTTP200与tool contract通过；组成是2 auth denials、2 warmups、24 perf calls（shared/managed各12、3轮每轮8）、1 stale-version denial、4 query-ingest并发前置查询、2 post-ingest readbacks。共享/managed fixture与两次新上传文档各18 chunks；marker/file/version readback均符合预期。性能轮每轮2.872/2.650/2.531秒。以原始单调用elapsed秒统计：shared n12 median/nearest-rank P50/P95/max=1060.71/1034.10/1587.86/1587.86ms；managed n12=1074.73/1028.74/1396.52/1396.52ms。

实际tool metadata显示shared性能查询12/12 `dense=10,bm25=10,fused=12,reranker_used=true`；managed性能查询12/12也召回10+10、融合12，但`reranker_used=false`；两次post-ingest managed readback fused=13且仍未重排。源码调用链确认shared service注入`shared_reranker`，managed file service建`RetrievalService`时没有传reranker。不得将独立reranker REST性能误写为managed MCP路径实际执行了重排。model events共有1106条、55 admitted operations：embedding query32、document8、rerank15；真实forward end分别32、8、90。单路CUDA活动lane峰值=1，因此MCP1没有证明多lane GPU overlap。MCP E2E只作整链路指标；模型子调用使用模型API事件直接测，不相减推断。

MCP2 `20261012T125923Z-b93726` 在MCP流量前因`ModelProviderError/model_identity_invalid`失败，没有MCP request rows；formal/frozen/model state unchanged，cleanup容器/卷/网络0。该失败启动既不算MCP2成功也不算MCP2行为失败。MCP报告还注明ready时间含自动warmup、OS cache已热，未测cold HTTP推理或磁盘冷启动。独立证据与口径见[independent-acceptance-report.md](independent-acceptance-report.md)。

## 主Agent最终汇总（本轮实测；2026-10-12）

最终直接API对照 `124501Z-fe800f` / `124736Z-eb9321`，同source151fbd、同image/model assets/精度/inputs/schedule，分别240成功/0失败，三family和document补测，均6serialwarmup后测量。query-only吞吐4.71→6.18（+31.2%）、rerank6.43→7.94（+23.5%）、mixed6.03→7.31（+21.2%）；document7.98→8.35但中位208.48→297.68ms。直接测得的排队减少、执行时间增加，不用E2E相减推断执行。独立Agent复算API1/2及真实HTTP boundary1/2通过；四路因两路显存free最低3798MiB且没有瞬时activation上界，安全停止加路，未启动未验证，不宣称四路OOM或4070不能承载。

正式RemoteModelClient hardcode admission.active==1，`125923Z-b93726` MCP2在seed前被model_identity_invalid拒绝，0 MCP工具调用。新增仅MCP冻结backend-src client差异：显式expected-lanes+adapter-lane身份绑定，default1不变，保留其他身份/schema/预算检查；模型服务仍151fbd不改，backend独立hash8e8fe804…f641。独立3项CPU identity合同通过。主Agent以同client副本重新对照：MCP1 `130402Z-ddb5c7`、MCP2 `130745Z-6ff9dd`，各35业务tools/call（24性能、11正确性/预热/竞争），HTTP200、3预期MCP拒绝、0非预期错误/503/超时；性能吞吐3.034→3.400req/s（+12.1%），中位1014.74→927.16ms、P95 1377.49→1209.73ms。模型峰值1/2，各55操作；两次query/ingest窗口在两路均有4个document与query/rerank forward overlap pairs。两个新18切片文件在任务完成后新发起读回精确file/version，前置query仍读取初始资料。

源码及输出明确shared dense10/BM2510/fused12、实际rerank12、Top5；managed同召回融合12但reranker_used=false、实际rerank0，因为FileManagementService构造RetrievalService未传reranker，管理_rw_lock/_model_lock仍串行保护快照及模型使用。不修改业务接线或锁。parent MCP到child模型ID没有贯通，parent排队/执行未测得，UTC窗口归属不冒充精确分布式trace。完整链路收益不能全部归因于模型并发。

独立子Agent在已完成API、取消边界、原MCP1和客户端合同复核后遇到额度限制，最终同client MCP1/2由主Agent运行及复算；**最终MCP2子Agent独立复核未完成**。这项限制保留在报告/当前结论，不用程序成功替代独立签收。

收尾验证：必要CPU测试41 passed；Ruff及AST，逻辑输入从冻结程序提取并与768条已记录模型请求hash逐条匹配（含失败/pilot，不混入480成功对照）；全部16轮冻结文件hash一致，116项正式业务/配置hash未变，原7容器ID仍运行。每轮cleanup均0容器/卷/网络且临时凭据目录删除；不存在run-state临时凭据引用文件。资源采样/冷进程启动局限见报告，未运行冻结集、无关完整测试、Redis/cache/dynamicbatch。

完整结论、统计（成功/失败/全请求分开）、原始证据入口、失败历史、重现命令、下一轮建议见 [final-report.md](final-report.md)、[final-evidence.json](final-evidence.json)。主环境使用uv，服务依次运行、每次prepare→freeze→run；各run的frozen/config及source/client-diff是实际配置/差异，不是事后设想。业务未修改；目录新增隔离experiment-source、分轮frozen快照、程序/合同、原始JSON/JSONL、脱敏Docker日志及汇总文档。code.md保持当前结论，docs/development-log.md更新索引。
