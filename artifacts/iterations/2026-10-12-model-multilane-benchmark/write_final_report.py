"""Render the final report from saved, recomputed evidence."""
import json
from pathlib import Path

base = Path(__file__).resolve().parent
d = json.loads((base/'final-evidence.json').read_text())
api = d['api']
subsets = d['api_embedding_subsets']
report = ['# 模型多路并发实验报告（2026-10-12，本轮实测）', '',
'## 结论', '',
'RTX 4070 SUPER 在本机固定0.6B模型负载下可以实际运行两路；直接模型API与完整MCP均观察到两个CUDA stream区间重叠，未出现OOM、非预期错误、数值漂移或模型张量状态变化。两路纯query向量化吞吐提升31.2%，纯重排提升23.5%，混合提升21.2%；完整MCP混合查询提升12.1%。这不是线性翻倍：同请求执行时间变长、显存使用增加，文档批向量化收益仅4.6%且中位响应更慢。', '',
'四路未启动、未验证。两路实测显存最低余3798MiB，而尚无四路瞬时activation/分配峰值上界；0.56秒左右采样无法保证峰值前及时停止，独立验收建议保留两路为本轮上限。没有四路OOM证据，不能说4070 SUPER带不动四路。决定见 [四路安全记录](four-lane-safety-decision.json)。', '',
'正式默认仍单路。建议继续两路的受控工程实验，先将锁/客户端身份/资源遥测边界设计成可验收实现，再决定是否采用；本轮不改正式业务实现、配置、默认值或检索参数。', '',
'## 分工、隔离与冻结', '',
'主Agent负责协调、审核、运行与汇总；GPT-6 Luna高强度子Agent分别开发测试程序和独立验收，实际请求均由程序产生。独立子Agent已复核API1/2、CPU合同、真实HTTP取消1/2、原完整MCP1及客户端补丁合同；其在最后MCP对照前遇到额度限制，最终MCP1/2由主Agent复算原始证据，**最终MCP2未完成子Agent独立复核**。见 [独立验收记录](independent-acceptance-report.md)、[主Agent复算](final-evidence.json)。', '',
'每次服务启动前保存frozen程序、源副本/差异、Compose解析配置、image IDs、GPU/模型身份、文件hash；临时随机Docker project/端口/账号/目录，Qdrant与数据卷独立，宿主映射全部127.0.0.1。只有一份Embedding实例与一份Reranker实例、一个model-node GPU容器，权重只读挂载。7个原项目容器保持原ID运行，116项业务源码/正式配置/lock文件hash全部未变。', '',
'最终模型源SHA `151fbd6e4d525d77807ebd9f655e4a2d0c33ef858e18d5ba8397c7c0fd5a994f`；正式源SHA `c0aa7dfcdb151c0f7f957a66b889bb3ba1406c4ffa1f3d5e671d6b7154f5a674`。模型image ID `sha256:c9d4e5172eca901fcec1d3389c8c119183eacd8b8788e4a3fbb8838dc2fdca3e`，backend `sha256:53e2dacc8798c137e0421b616219b9025143f944003e6d51f3b0d3e5c2d6a066`。', '',
'模型API实现身份 `8ced4e42fce3a0dc57890e3ea2e965d9c88dc562cc246437be72057e8a706d0f`；Embedding `Qwen3-Embedding-0.6B`，asset `5ae1f47974e5cf691019d4e5dd1bcd898bd3af4988cec056a01eebef2617ae5b`，1024维/归一化，精度为相同后端默认（未显式改精度）。Reranker `Qwen3-Reranker-0.6B`，asset `6ad4361283792c8ecdadb5fb5d7e3ed34866dc869238d76ea4c66a3531525a99`，float16、max_length1024、batch2。依赖torch2.11.0+cu128/Transformers4.57.6/ST5.7.0/HF0.8.0，见每run的model-identity.json。', '',
'## 锁、真实并发及客户端入口', '',
'正式服务只有单worker FIFO及整个embedding/rerank调用的runtime RLock，没有同时推理路数配置；max_waiting8和wait30秒只是队列边界。runtime锁同时保护encode的to/eval等共享状态并限制推理，因此不能直接删锁。本轮独立源码副本保留固定设备/eval/dtype与默认RoPE/dropout0/cache保护、Embedding preprocess锁、Reranker tokenize/pad锁，以及shutdown drain后的生命周期锁；只将受保护的forward放入每lane独立CUDA stream，并等待CUDA结束才归还名额。张量完整hash在drain前后相同。适配器不是任意模型的通用线程安全保证。', '',
'直接API1/2真实CUDA peak分别1/2，2路跨lane415个forward区间pair，跨Embedding/Reranker有9个不同request pair；同lane无forward重叠。CUDA event区间重叠证明两个stream工作区间同时存在；未测得SM/kernel级并行比例、PCIe带宽、功耗/温度或内核级瓶颈。客户端在途始终4，不能拿4个HTTP请求当4路推理。', '',
'正式RemoteModelClient还把identity.admission.active固定要求1，MCP2首次被model_identity_invalid拒绝。最终MCP在另一个冻结backend-src客户端副本中仅加显式expected lanes绑定，非1档还须experiment_adapter.lane_count一致；所有模型/asset/implementation/schema/超时校验保留。模型服务仍使用上述相同源151fbd；单路和两路MCP使用相同backend SHA `8e8fe804c18bc6e085816e03b8a11a58680547e7b286da63ced5857eaa52f641`。差异见最终MCP run的frozen/backend-client-diff.patch。正式客户端仍拒绝多路。', '',
'管理层FileManagementService的_rw_lock保护版本快照/数据一致性，_model_lock还串行化受管查询和建库模型调用；本轮保留。准确锁等待时间未测得，不从MCP总耗时相减猜测。', '',
'## 第一阶段：直接模型API', '',
'每档6条串行代表输入warmup全部验证后再测量240请求，三轮：query短/长各24，document短/长各24（补充建库负载），rerank4/8候选各24；混合96请求，每波2 embedding+2 rerank。HTTP inputs固定4；query实际每文本一个forward（每请求4个），document为一批4（每请求1个）；4/8重排候选对应batch2的2/4个forward。短/长与多候选输入保存为JSON并逐条校验其hash与client记录相同。', '',
'下面吞吐均为HTTP请求/秒，连续phase最早client start到最晚client completion作为持续时间；成功/失败分开，全请求统计仍保留。中位数用statistics.median，P95为nearest-rank；不是近似P50冒充中位数。所有有效matrix请求HTTP200，无503/超时/传输错误，失败样本n=0，因此失败耗时分位数未测得（无失败样本），全请求=成功请求。', '',
'| 负载 | 路数 | 请求/成功/失败 | 持续秒 | 吞吐req/s | 客户端中位/P95/最大ms | 队列中位/P95/最大ms | 执行中位/P95/最大ms |',
'|---|---:|---:|---:|---:|---|---|---|']
def triple(s):
    return '/'.join(f'{s[k]:.2f}' for k in ('median','p95','max'))
for i in range(2):
    for label, stats in [('query向量化',subsets[i]['query']),('document补测',subsets[i]['document']),('纯重排',api[i]['families']['pure_rerank']),('混合',api[i]['families']['mixed'])]:
        n=stats.get('request_n')
        span=stats.get('phase_duration_seconds',stats.get('family_phase_window_seconds'))
        rps=stats.get('successful_rps',stats.get('family_phase_successful_rps'))
        execution=stats.get('execution_ms',stats.get('operation_ms'))
        report.append(f'| {label} | {i+1} | {n}/{n}/0 | {span:.3f} | {rps:.3f} | {triple(stats["client_e2e_ms"])} | {triple(stats["queue_ms"])} | {triple(execution)} |')
report += ['', '队列来自admission_enqueued→worker_dequeued，执行来自operation_enter→operation_exit，服务端E2E来自request_received→request_completed；均直接测得。执行包含CPU预处理、固定批次的forward及同步/结果转换，不等于纯GPU kernel时间。每表行的n适用于其所有耗时统计。更细的短/长、group/round三轮结果、server E2E及nearest-rank P50见 [独立比较](independent-comparison.md) 和 [完整复算JSON](final-evidence.json)。冻结summary中的纯family/round窗口因group外层循环跨越其他轮，不能当独立轮吞吐；本报告使用连续phase或各group/round真实窗口。', '',
'两路所有向量/分数与单路逐值max-abs=0，cosine/L2检查在容差内，重排顺序/top3相同，无结果错位，模型前后hash一致。本轮容差embedding1e-4、rerank1e-3；该相同结果不构成未来所有输入的保证。', '',
'两路query排队中位337.05→68.46ms，但执行中位195.77→281.00ms；模型并发降低排队，却使共享GPU上的单次执行变慢。document吞吐仅7.98→8.35（+4.6%），响应中位208.48→297.68ms，不能建议它普遍提速。两路GPU用量峰值比单路增加979MiB。现有执行延长与设备竞争相符，具体compute/memory-bandwidth瓶颈未测得，不能只凭100%利用率归因。', '',
'## 第二阶段：完整MCP', '',
'最终对照run：[MCP1](runs/20261012T130402Z-ddb5c7/mcp-summary.json)、[MCP2](runs/20261012T130745Z-6ff9dd/mcp-summary.json)。每档6个独立会话（A4/B2），保留MCP2025-11-25 initialize/initialized/tools-list、协商及会话header；不同用户独立临时凭据。每档35 tools/call均HTTP200：24性能查询+2授权拒绝+2warmup+1过期version拒绝+4查询/建库竞争前置query+2建库完成后的新读回。32次证据查询成功，3次预期拒绝无证据；预期MCP错误file_not_available/forbidden/invalid_target各1，不算系统失败。非预期错误、503、超时0。', '',
'共享与初始受管资料各18个切片；候选10 dense+10 BM25、融合12、返回Top5。共享链路：MCP鉴权/target路由→query embedding API→Qdrant+BM25→RRF→12候选reranker API→证据身份校验。受管链路：MCP个人鉴权→管理桥→文件/版本快照及管理锁→query embedding API→Qdrant+BM25→RRF→Top5证据校验；实际reranker_used=false、重排0，因为构造RetrievalService未传reranker。建库后新文件融合13，仍重排0。没有调整candidate_k10/rerank_pool30/top_k5/8000字符预算。', '',
'| 档位 | 性能请求成功/失败 | 三轮总秒 | 吞吐req/s | 客户端中位/P95/最大ms | shared中位/P95ms（n12） | managed中位/P95ms（n12） |',
'|---|---:|---:|---:|---|---|---|']
for i,m in enumerate(d['mcp']):
    report.append(f'| {i+1} | 24/0 | {m["performance_duration_seconds"]:.3f} | {m["successful_rps"]:.3f} | {triple(m["client_e2e_ms"])} | {m["labels"]["shared"]["client_e2e_ms"]["median"]:.2f}/{m["labels"]["shared"]["client_e2e_ms"]["p95"]:.2f} | {m["labels"]["managed"]["client_e2e_ms"]["median"]:.2f}/{m["labels"]["managed"]["client_e2e_ms"]["p95"]:.2f} |')
report += ['', '每档三轮各8个查询，客户端在途4。两路全生命周期模型55个操作（query32/document8/rerank15），CUDA峰值2；单路同55操作，峰值1。每档两次查询+建库竞争窗口均含query3/document2/rerank1（包含完成后的新读回），两路各观测4个建库document与query/rerank CUDA重叠pair，单路0。操作前已开始的查询仍针对初始file/version；两次ingest任务成功后才grant并新发起读回新file/version，不把前置query当成读取新版本。', '',
'本轮MCP吞吐约提升12.1%，小于API收益。受管管理锁、召回/融合/身份与HTTP/MCP协议都有额外工作；源码表明串行环节仍存在，但它们各自的耗时/锁排队未测得，不能把完整提升全部归因于模型并发。各parent MCP请求的模型排队、执行时间未测得：没有传播parent关联ID；JSON保留模型子调用直接排队/执行统计（全生命周期n55，含准备/建库），不能冒充每个parent耗时。竞争窗口按UTC时间归属，跨宿主/容器时钟偏差未测得；CUDA重叠在同模型进程内用事件原点计算。', '',
'## 正确性与边界', '',
'| 场景 | 结果 | 范围 |',
'|---|---|---|',
'| 两路实际推理/跨模型重叠 | 通过 | 模型API及最终MCP均有同进程CUDA事件；未测kernel级并行 |',
'| 数值/数量/维度/norm/排序/模型状态 | 通过 | 固定API480条测量；结束张量hash一致 |',
'| 用户A授权/用户B独立凭据拒绝 | 通过 | 每个MCP档shared/managed各一次B拒绝且无证据 |',
'| 文件、版本、target及正文证据身份 | 通过 | 两档正向证据全部校验；过期version拒绝；完成后的新file读回 |',
'| 替换/删除/撤权竞态 | 未验证 | 本轮未做这些变更，不引用历史baseline为本轮通过 |',
'| 队列满/等待超时 | 通过 | 1/2档真实HTTP，专门max_waiting1/wait0.2s；503错误各对应事件 |',
'| 排队取消/执行断连名额释放 | 通过 | 1/2档CPU hold与真实GPU probe；active_disconnect后forward继续到结束才drain，fresh成功 |',
'| 四路安全性/收益 | 未验证 | 资源预算不足以安全证明，未启动 |',
'| 最终MCP2子Agent独立复核 | 未完成 | 子Agent额度限制；主Agent已重算/审核，证据保留 |', '',
'边界run1共11HTTP：200×4、queue_full503×1、queue_timeout503×3、主动取消×3；run2共15HTTP：200×8、相同4个503与3个取消。这些为预期受控拒绝，不计性能失败率。CPU hold只检验admission状态机，不代表GPU耗时。真实GPU各12query输入，服务识别disconnect后仍有12个forward_end，operation_exit后才lane_drained；不假定断连立即停止GPU。全/成功/预期拒绝及取消终止时间分位数、样本与持续时间见final-evidence.json的boundary字段及 [边界1](runs/20261012T124331Z-e77826/boundary-summary.json)/[边界2](runs/20261012T125116Z-ee4f61/boundary-summary.json)。', '',
'## 资源与停止条件', '',
'开始门槛GPU free8192MiB、宿主free6144MiB；停止GPU free<1024MiB或宿主free<3072MiB、OOM、指标获取失败、连续容器重启>=2。名义GPU/宿主0.5秒，Docker2秒；实际command耗时使采样间隔更大。未触发资源停止或OOM；不宣称采样捕获了瞬时峰值。CPU是Docker多核CPU百分比，未设置CPU硬停止线；宿主CPU、GPU每kernel占用/activation上界未测得。', '',
'| 轮次 | GPU/宿主样本 | 实际间隔中位/最大ms | Docker样本/间隔中位/最大ms | GPU已用峰值MiB/利用率峰值 | model-node内存峰值MiB/CPU峰值% | 宿主空闲最低MiB |',
'|---|---|---|---|---|---|---|']
for label,r in [(f'API{i+1}',r) for i,r in enumerate(d['api_resources'])]+[(f'MCP{i+1}',m['resources']) for i,m in enumerate(d['mcp'])]:
    report.append(f'| {label} | {r["gpu_host_samples"]} | {r["gpu_host_actual_interval_ms"]["median"]:.0f}/{r["gpu_host_actual_interval_ms"]["max"]:.0f} | {r["docker_samples"]}/{r["docker_actual_interval_ms"]["median"]:.0f}/{r["docker_actual_interval_ms"]["max"]:.0f} | {r["max_gpu_used_mib"]}/{r["max_gpu_utilization_percent"]}% | {r["max_model_container_memory_mib"]:.1f}/{r["max_model_cpu_percent"]:.2f} | {r["min_host_free_mib"]} |')
report += ['', 'GPU统计含桌面/其他现有工作负载，不是本项目独占用量；model-node内存来自Docker、宿主free来自Windows，两者不能简单相加推算。Docker约4–4.8秒采样很粗，短峰值/CPU极值可能遗漏；GPU也有0.59–0.72秒最大间隙。prepared.json的旧helper门槛描述已过时，以冻结runner及effective-resource-thresholds.json的1024/3072实际门槛为准。', '',
'进程启动到ready（含加载/自动短warmup/状态fingerprint）：API1 56.35s、API2 55.55s、最终MCP1 54.24s、MCP2 53.31s。冷启动单独记录，不混入吞吐；OS文件缓存已被此前运行预热，磁盘真正冷启动未测得；服务启动自动warmup，未测未预热的首个业务推理耗时。随后另串行验证全部6个代表输入warmup，见warmup-*.json/JSONL。', '',
'## 失败及准备快照保留', '',
'| run | 分类 | 实际证据/处理 |',
'|---|---|---|',
'| 121105Z-2cb7de / 121238Z-45c9be | prepare-only | 没有启动服务，旧程序快照被取代；资源0、临时凭据清理 |',
'| 121422Z-b42d0b | 启动失败 | Starlette无middleware装饰器，0性能请求；实验入口修兼容注册 |',
'| 122345Z-5d8d9d | 启动失败 | _parallel_process检查错误对象，0性能请求；核实HFE/ST对象及encode参数保护 |',
'| 123014Z-42bd9d | 测试协议失败 | 144 HTTP400 invalid_request，0forward；REST误用MCP日期，不能作为基线 |',
'| 123702Z-c0ed92 | 通过pilot | 144/144成功，但缺独立pure rerank对照；不是最终240矩阵 |',
'| 123936Z-a79495 | 边界失败 | 5HTTP，queued cancel被观察成timeout；BaseHTTPMiddleware改纯ASGI透传后新轮通过 |',
'| 125406Z-4a9f0c | MCP partial失败 | 31个toolcall合同通过，new file未同步授权资源grant404；补GET inventory后重新冻结 |',
'| 125703Z-2d27c2 | MCP1前版通过 | 35call，正式client单路；client实验副本形成后重新做最终同入口对照 |',
'| 125923Z-b93726 | MCP2入口拒绝 | 正式client active==1身份检查，0 MCP调用；隔离client显式lane绑定后新轮通过 |', '',
'全部16个prepare/run的冻结文件hash均复核一致，cleanup记录均0容器/卷/网络且临时凭据目录删除。失效轮不补写为成功；setup问题属于测试入口/适配边界，不是模型性能失败。上述业务线差异（受管无重排、管理锁、client只接受单路）仅记录原因与建议，不改正式实现。', '',
'## 可复现入口、证据与后续建议', '',
'运行位置为项目根目录，主环境用uv；本轮直接命令与配置冻结在每run，secret通过临时文件路径注入且不进入artifact。典型重现命令（将prepare输出的实际state路径传给run）：', '',
'```powershell',
'uv run --no-sync python artifacts/iterations/2026-10-12-model-multilane-benchmark/experiment_driver.py prepare --lanes 1 --phase model --rounds 3',
'uv run --no-sync python artifacts/iterations/2026-10-12-model-multilane-benchmark/experiment_driver.py run --state <本次prepare返回的state路径>',
'uv run --no-sync python artifacts/iterations/2026-10-12-model-multilane-benchmark/experiment_driver.py prepare --lanes 2 --phase model --rounds 3 --reference <新的单路run/reference.json>',
'# boundary分别使用1/2；MCP也分别使用1/2；每run先prepare冻结再run，模型容器依次运行',
'uv run --no-sync python artifacts/iterations/2026-10-12-model-multilane-benchmark/experiment_driver.py prepare --lanes 2 --phase boundary --rounds 3',
'uv run --no-sync python artifacts/iterations/2026-10-12-model-multilane-benchmark/experiment_driver.py prepare --lanes 2 --phase mcp --rounds 3',
'```', '',
'正式可复算结果：API [1](runs/20261012T124501Z-fe800f/summary.json)/[2](runs/20261012T124736Z-eb9321/summary.json)，MCP [1](runs/20261012T130402Z-ddb5c7/mcp-summary.json)/[2](runs/20261012T130745Z-6ff9dd/mcp-summary.json)。每run保留client请求/原始向量分数、模型事件、资源JSONL、脱敏Docker日志、identity、freeze、cleanup及outcome。inputs-and-model-payloads.json由冻结生成器提取逻辑payload，并与实际记录input_sha256逐条一致；所有客户端请求关联ID的回显与服务端状态检查通过。MCP parent到model child关联未接入，不能伪装成完整分布式trace。', '',
'41项必要CPU/输入/队列/客户端合同通过，Ruff和AST检查通过；未运行冻结评测集或无关完整测试。没有Redis、语义缓存、动态batch或检索参数调整。按证据排序的下一步：', '',
'1. 先补CUDA allocated/reserved峰值遥测、OOM/瞬态分配预算与停止机制，再决定四路是否安全。现有free采样不能提供每请求activation上界。',
'2. 两路具备API实际收益，但文档批负载时延变差；按query/rerank/ingest负载继续验证公平性、slot取消与工作负载差异，再形成正式可配置方案及client协议边界，不自动改默认。',
'3. 分开评估管理_rw_lock/_model_lock的安全保护范围及等待时间；保留版本/授权一致性，用证据确认串行部分可否细化。MCP只提升12.1%，尚未测具体锁等待，不直接删锁。',
'4. 明确受管检索是否应接入reranker及其契约；本轮未调整效果。补MCP parent/child关联与最终MCP2独立复核，再扩大重复次数/代表资料形态。', '',
'本机Windows宿主、Linux Docker及loopback测试，不代表真实内网、WAN/VPN或生产容量验收。模型是0.6B，不外推8B；只有客户端在途4、三轮小样本、固定合成18切片和短/长输入，未测多机、多副本、实际TLS网络、长时稳定性或真实用户流量。', '']
(base/'final-report.md').write_text('\n'.join(report),encoding='utf-8')
print('final-report.md written')
