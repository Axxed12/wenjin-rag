# 持续开发记录索引

2026-10-17 [团队交付开发保存并合入main](../artifacts/iterations/2026-10-17-main-integration/development-log.md)：原修改与已存基线一致，两次ff-only合入，无业务源码冲突；复用交付分支验收，未推送/部署。

2026-10-17 [内网团队交付补齐](../artifacts/iterations/2026-10-17-team-delivery/development-log.md)：私域MCP、受管重排、协作取消/锁计时、三镜像与Docker业务恢复、GPT取证回答；[交付报告](../artifacts/iterations/2026-10-17-team-delivery/final-report.md)区分独立合同、CLI连接与待人工项目。

2026-10-15 [评测界面可读性修复](../artifacts/iterations/2026-10-15-evaluation-readable-ui/development-log.md)：修复空表格对象字符串、内部标识收进详情、状态去重及换行；51项前端行为检查与真实静态资源检查通过。

2026-10-15 [路线页状态同步](../artifacts/iterations/2026-10-15-roadmap-status/development-log.md)：按当前实现区分已实现与待完善，更新账号说明；仅静态文案，未新增后台功能或重跑业务验收。

2026-10-14 [MCP入口页面、本机控制服务与身份桥接](../artifacts/iterations/2026-10-14-mcp-entry-control-ui-deploy/development-log.md)：两版入口卡、连接配置、推荐默认及可轮询启停；本机broker与原生管理进程接线。跨Windows/Linux身份库WAL失败后，按用户追加授权采用逐请求HTTP身份桥接；实现、实际部署和客户端验收边界分别记录。

2026-10-14 [MCP入口管理后端](../artifacts/iterations/2026-10-14-mcp-entry-control/development-log.md)：新增连接profile/default CAS、管理员session+CSRF路由、固定Docker broker与可轮询启停operation。仅broker启动时明确配置的Compose项目中两个MCP服务可控；管理容器无Docker socket。29项合成合同、8项相关回归及静态检查通过；未部署或操作真实容器。

2026-10-14 [无状态MCP实际合入main](../artifacts/iterations/2026-10-14-mcp-stateless-integration/final-report.md)：从90a7e1f起选择性整合来源提交，兼容当前模型错误/超时/个人授权，独立验收后ff-only合入；保留新旧依赖和服务隔离，未推送部署，未执行本轮容器/HTTPS或真实模型验收。

2026-10-14 [新版无状态MCP业务整合](../artifacts/iterations/2026-10-14-mcp-stateless-integration/development-log.md)：typed ToolResult与协议翻译、WAL只读身份库、当前超时/队列预算配置完成整合；官方TS SDK网络、新旧协议隔离及授权/版本/read-only合同16项+6 subtests通过，受影响旧环境合同8项通过。合成Compose JSON配置核对通过；本轮未做容器TLS、GPU、真实数据或生产验收。独立测试记录由测试Agent维护于同轮`test-development-log.md`。来源容器/SDK记录见[2026-10-09历史迭代](../artifacts/iterations/2026-10-09-mcp-stateless/development-log.md)，不得视作本轮实测。

2026-10-14 [当前开发本地Git保存](../artifacts/iterations/2026-10-14-git-checkpoint/development-log.md)：保存当前代码、配置、测试及开发记录，排除运行数据库/凭据/原文，不重跑测试或推送远端；验收沿用各轮原有证据。

2026-10-14 [MCP凭据管理与北京时间](../artifacts/iterations/2026-10-14-mcp-credentials-ux/development-log.md)：管理员按所属账号统一管理，签发后一次复制交付、按名称/前缀/状态识别与确认撤销；全部时间固定北京时间，390px卡片显示。最终Node42项、隔离后端6项通过，真实页面只读查看与桌面/390px复查；恢复原18777/18765服务，9条凭据与22条授权摘要不变，未签发或撤销真实token，真实客户端分发流程未验收。

2026-10-13 [授权编辑器简化与统一“全部”文案](../artifacts/iterations/2026-10-13-grant-editor-simplification/development-log.md)：资料库折叠行与单一普通用户勾选清单，保存仅提交差异，保留CAS/账号隔离/目录同步；管理员历史授权不修改。Node35项、独立34项与HTTP1项通过，桌面/390px真实页面和草稿取消检查完成；未写真实授权，刷新加载静态资源。

2026-10-13 [文件管理类型筛选与SVG展开箭头](../artifacts/iterations/2026-10-13-library-filter-chevron/development-log.md)：默认空“受管文件”分组隐藏，保留实际文件与主动创建空库；来源筛选换为公开/私有分类，箭头居中SVG统一。Node33项通过；真实页面公开/私有与搜索组合检查，窄屏筛选框溢出修复。仅静态前端，未更改真实数据或授权。

2026-10-13 [资料库分类、管理员读取与实际服务加载](../artifacts/iterations/2026-10-12-library-visibility-consistency/development-log.md)（10月12启动）：公开/私有仅分类，管理员可读全部有效库，普通账号授权不变；分类CAS与独立元数据迁移、可管理副本说明和折叠布局修复。定向后端24项、独立22项、最终Node32项通过；重载确认的18777/18765，真实授权摘要和模型身份不变；浏览器AI库发现/真实检索、桌面与390px文件页复查完成，完整管理生命周期与真实私域原生MCP仍未验收。

2026-10-12 [全局账号入口与私域目录登记](../artifacts/iterations/2026-10-12-admin-session-private-catalog/development-log.md)：私域显式路由选择、资源同步、管理员发现与读取权限分离；顶栏切换/退出及撤权证据清理。真实目标只读完整准入PASS，隔离后端14项、Node30项通过；未改真实授权、未重启运行服务，浏览器验收待做。

2026-10-12 [本机手动Agent服务启动](../artifacts/iterations/2026-10-12-manual-agent-service/development-log.md)：管理/MCP原生后台进程及唯一正式单路模型容器已启动；新身份库无管理员/授权，健康、匿名拒绝、embedding/reranker调用通过，用户交互bootstrap后继续个人MCP验收。

2026-10-12 [模型多路并发实验报告](../artifacts/iterations/2026-10-12-model-multilane-benchmark/final-report.md)：1/2路直接模型API与完整MCP实测完成；两路query/重排/混合API吞吐+31.2%/+23.5%/+21.2%，MCP+12.1%；受管链路未重排、管理锁保留。四路无安全瞬时显存预算，未启动未验证；正式默认仍单路，临时资源/凭据已清理。最终MCP2子Agent独立复核因额度未完成，主Agent已复算；完整失败与冻结记录见当轮日志。

2026-10-12 [第一轮可复现并发基线](../artifacts/iterations/2026-10-12-concurrency-baseline/development-log.md)：独立Docker、临时账号/语料/索引、真实GPU模型API与状态化MCP；1/2/4/8并发矩阵完成，原始数据与测试程序保留。跨链路模型忙503与取消会话异常已实测；授权/替换/撤权/删除后新请求通过，临时资源已清理，详见当轮报告；非真实内网或生产容量验收。

2026-10-08 [本机与云端双部署配置及验收](../artifacts/iterations/2026-10-08-dual-deployment/development-log.md)：保留root Compose兼容入口，新增backend/ML分层和local/cloud/model-node配置；正式backend锁定依赖构建成功，三种Compose配置校验通过，MCP私网HTTP guard 4项与正式backend+Caddy TLS登录/状态化MCP合同1项通过；另有缓存ML依赖源码镜像完成一次CUDA embedding/rerank API smoke。TLS合同不含检索/上传；Linux原生secret权限、完整ML target从零构建、真实云/WAN和业务数据恢复仍未验收。操作步骤见[双部署说明](dual-deployment.md)和[独立测试记录](../artifacts/iterations/2026-10-08-dual-deployment/test-development-log.md)。


2026-10-08 [本轮汇总审核与中断定位](../artifacts/iterations/2026-10-08-iteration-review/development-log.md)：记录目标分工与额度恢复后三名Agent继续推进。隔离合同覆盖profile seal/模型身份一致性；同一临时TCP生命周期验证stateful MCP的shared release切换/取件、managed上传/替换/版本取件和即时撤权。详见[发布测试记录](../artifacts/iterations/2026-10-07-library-release-management/test-development-log.md)末尾；不代表真实数据或生产验收。固定开发集四臂和selected正文复核已完成；评测API合成E2E与前端行为合同通过，浏览器手工验收仍待进行。

目标1当前批次（2026-10-08）：[资料库发布版本管理实现与验证](../artifacts/iterations/2026-10-07-library-release-management/development-log.md)。发布domain/HTTP/CAS、sealed profile继承与篡改拒绝、同维度错模型拒绝、Settings显式Embedding ID统一均有隔离合同；stateful MCP loopback TCP临时合同覆盖shared发布/取件、managed上传/替换/旧版与新版取件、实时撤权。细节见[测试记录](../artifacts/iterations/2026-10-07-library-release-management/test-development-log.md)末尾；无真实资产、冻结评测或生产服务验收。

目标3设计及runner首轮实现（2026-10-08）：[检索策略对照设计与实现记录](../artifacts/iterations/2026-10-08-retrieval-strategy-design/development-log.md)。四臂设计已审核；target pin/fingerprint helper、可校验checkpoint runner和source-native parent、多查询预算、MMR向量合同已有9项独立synthetic合同。真实GDAL baseline、单题和33题固定开发集四臂已运行；28正向case/55 facts的selected正文复核见[矩阵](../artifacts/iterations/2026-10-08-retrieval-strategy-experiment/full-developer-four-arm/selected-body-review.md)。该开发集上multi-query 55/55、baseline 54/55，不能外推为生产胜负；baseline继续默认，不在同一开发集调参。评测管理app CPU合成E2E与权限/前端行为合同见[任务实现记录](../artifacts/iterations/2026-10-08-evaluation-tasks/development-log.md)，浏览器验收仍待进行。

目标3当前GDAL及开发集实验（2026-10-08）：[GDAL当前链复核与四臂实验](../artifacts/iterations/2026-10-08-retrieval-strategy-experiment/development-log.md)。程序经项目Settings私有加载完成真实当前链baseline、同题四臂及33题固定开发集四臂；逐题候选/selected正文、locator、hash与固定模型/资产/代码身份均保留。单题独立正文检查区分rank3字面提及和rank5真实定义，单题MMR漏精确定义；这不是总体质量结论。全题逐题语义审核待完成，未改真实索引或默认策略。

目标4管理评测接线（2026-10-08）：[管理员评测任务实现记录](../artifacts/iterations/2026-10-08-evaluation-tasks/development-log.md)及[定向测试记录](../artifacts/iterations/2026-10-08-evaluation-tasks/test-development-log.md)。本地账号模式的独立SQLite任务服务、固定模板API、进度/取消/恢复/结果、审计及管理员前端已接入；默认管理app的CPU合成HTTP submit→poll→result合同及权限/前端Node合同通过。33题真实四臂runner由共用factory/context与runner通过artifact脚本执行，非HTTP端到端。浏览器人工验收仍待进行。

新goal第一批设计（2026-10-07）：[资料库发布版本管理只读设计核对](../artifacts/iterations/2026-10-07-library-release-management/development-log.md)。仅源码审查与方案，待root审核，未修改产品代码、未读真实资产或运行服务；用户授权的四项目标保持完整。

最新前端修复（2026-10-07）：[上传完成后检索目录同步](../artifacts/iterations/2026-10-07-upload-catalog-refresh/development-log.md)。用户确认“新上传文件无需刷新就会出现在检索调试中”；与“完整流程及390px都试过”的先前反馈共同作为本轮用户手工验收来源，非Agent观察且无新增截图。6项JS行为测试和隔离HTTP静态SHA亦通过；用户未逐项描述的断言不作扩展。

最新文档维护：2026-10-07 [四批结构重构范围与总体证据](../artifacts/iterations/2026-10-07-structure-refactor/scope.md)、[总体开发记录](../artifacts/iterations/2026-10-07-structure-refactor/development-log.md)、[入口实现与测试记录](../artifacts/iterations/2026-10-07-structure-refactor/entry-development-log.md)。B-D源码/目录批次按原定范围完成 scoped evidence；A有用户完整流程与390px反馈，另确认新上传无需刷新即显示，见最新目录刷新记录。集中回归首轮失败和后续定向修复分开记录，不把collect-only 644写成测试通过。

当前结构重构（2026-10-07）：上述[总体记录](../artifacts/iterations/2026-10-07-structure-refactor/development-log.md)和[目录布局](project-layout.md)汇总HTTP/MCP真实stateful TCP、安装态wheel、生命周期16项及D目录/marker证据与回归失败/修复范围。B-D原定scoped验证已完成；A由用户反馈确认完整流程与390px曾手工尝试，并确认新上传无需刷新即出现在检索调试。结构重构wheel仅属当批安装态资源/入口证据，不代表本轮JS；本轮JS以6项行为测试和隔离HTTP静态SHA核验为据。

重构前管理界面基线（部分尝试，2026-10-07）：[UI记录](../artifacts/iterations/2026-10-07-structure-refactor/ui/development-log.md)、[验收状态](../artifacts/iterations/2026-10-07-structure-refactor/ui/acceptance.json)。自动安全层终止Computer Use，后续流程未验证。

最近功能迭代：2026-10-07 管理前端六项反馈，[开发记录](../artifacts/iterations/2026-10-07-admin-improvements/development-log.md)、[真实7库14项检索](../artifacts/iterations/2026-10-07-admin-improvements/real-corpus-acceptance.json)、[隔离HTTP](../artifacts/iterations/2026-10-07-admin-improvements/http-acceptance.json)、[上传归属与管理入口快照](../artifacts/iterations/2026-10-07-admin-improvements/http-upload-and-snapshot.json)、[本机8777复验](../artifacts/iterations/2026-10-07-admin-improvements/deployed-acceptance.json)、[最终检查](../artifacts/iterations/2026-10-07-admin-improvements/final-checks.json)。日志覆盖、浮点指纹边界和浏览器未复验项见开发记录。

历史前端首轮：2026-10-06 RAG管理员前端，[开发记录](../artifacts/iterations/2026-10-06-admin-frontend/development-log.md)、[36项隔离HTTP验收](../artifacts/iterations/2026-10-06-admin-frontend/http-acceptance.jsonl)、[最终源码/wheel检查](../artifacts/iterations/2026-10-06-admin-frontend/final-checks.json)、[使用说明](admin-frontend.md)。浏览器文件选择自动化超时的未验证范围、模型/部署边界详见当轮开发记录，不作为本轮实测。

上一轮：2026-10-06 OSG active路由与managed文件MCP接入，[统一开发记录](../artifacts/iterations/2026-10-06-osg-managed-mcp/development-log.md)、[28项live验收](../artifacts/iterations/2026-10-06-osg-managed-mcp/acceptance.md)、[独立证据复核](../artifacts/iterations/2026-10-06-osg-managed-mcp/independent-review.md)。OSG与managed文件live runs、指纹范围和边界以这些当前记录为准。

上一轮文件管理核心：[`2026-10-06-file-management`](../artifacts/iterations/2026-10-06-file-management/development-log.md)（统一问题、原因、处理与证据）；[验收结果](../artifacts/iterations/2026-10-06-file-management/acceptance-results.md)汇总12/12隔离HTTP cases、13项核心聚焦回归和5项独立入口合同测试。

核心实现与安全范围详见[核心开发记录](../artifacts/iterations/2026-10-06-file-management/core-development-log.md)。

个人学习PDF导入记录：[`2026-10-06-ai-agents-pdf-import`](../artifacts/iterations/2026-10-06-ai-agents-pdf-import/development-log.md)。

检索迭代记录：[`2026-10-04-retrieval-validation`](../artifacts/iterations/2026-10-04-retrieval-validation/development-log.md)。

后续迭代按日期和主题在 `artifacts/iterations/` 追加详细记录；每轮同步维护 `code.md` 当前状态。历史复盘必须链接已有产物并标明历史来源。

2026-10-12 [模型推理有界准入与错误传递](../artifacts/iterations/2026-10-12-model-admission/development-log.md)：统一embedding/rerank FIFO准入、排队/断开/shutdown生命周期、超时预算及HTTP/MCP typed error接线；remote模式启动时要求管理桥read预算严格大于单次模型client read。独立24项准入/配置/HTTP/MCP合同、另批24项remote-provider/managed-bridge回归、stateful MCP Docker wire与GPU/Docker targeted run均按各自范围通过；两批24项不合并计数，query+ingest仅证明客户端操作区间重叠，服务端排队/推理竞争未观测。非全量/生产验收。关联修前证据：[并发基线最终报告](../artifacts/iterations/2026-10-12-concurrency-baseline/final-report.md)，[GPU targeted结果](../artifacts/iterations/2026-10-12-model-admission/runs/20261012T084409Z-6be28c/targeted-results.json)，独立测试：[测试记录](../artifacts/iterations/2026-10-12-model-admission/test-development-log.md)。

2026-10-08 [管理/MCP检索入口配置一致与候选深度对照](../artifacts/iterations/2026-10-08-retrieval-entry-parity/development-log.md)：两入口统一SearchPolicy/reranker生命周期；10/20、20/25两组每臂均54/55正向facts full，20/25 selected序列完全相同，25未产生可见Top5增益。线上当前base10、评测模板仍20；单次顺序计时不作为候选深度优劣证据。[20/25正文矩阵](../artifacts/iterations/2026-10-08-retrieval-entry-parity/candidate-20-vs-25/selected-body-fact-review.md)与[独立测试记录](../artifacts/iterations/2026-10-08-retrieval-entry-parity/test-development-log.md)。


2026-10-08 [管理账号、授权与评测体验修复](../artifacts/iterations/2026-10-08-admin-auth-eval-ux/development-log.md)：强制改密/会话边界、按库多用户授权、目录授权刷新与撤权清证据、评测授权提示和中文界面文案。Node 8项与临时HTTP 2项定向合同通过；浏览器桥接未提供可读tab，390px视觉和新Python后端服务运行未验收。见[独立测试记录](../artifacts/iterations/2026-10-08-admin-auth-eval-ux/test-development-log.md)。
2026-10-08 [管理页可读信息与资料库上传体验](../artifacts/iterations/2026-10-08-admin-readable-upload-ux/development-log.md)：新建逻辑资料库、独立保存原文件名与显示名、任务历史名称快照、中文标签、授权表与上传取消行为；Node主组25项、其后3项窄合同及隔离HTTP 1项通过。实际浏览器和390px尚未验收，服务未重启，页面部署状态待确认。

2026-10-08 [受管文件版本一致性与任务恢复](../artifacts/iterations/2026-10-08-backend-consistency-recovery/development-log.md)：replace候选独立保存元数据并在激活事务中切换；同job并发claim、失败候选取代/防旧版本覆盖、重启恢复及managed MCP库/版本授权竞态有17项临时定向合同通过。真实数据与服务未触碰；受管旧版回退API未实现。该后续批次目标1+2完成所述定向验收，目标3真实评测API链路仍在推进，目标4内网交付依用户要求暂缓。详见[独立测试](../artifacts/iterations/2026-10-08-backend-consistency-recovery/test-development-log.md)和[审核记录](../artifacts/iterations/2026-10-08-backend-consistency-recovery/review-log.md)。

2026-10-08 [管理员评测真实模型尝试与资源归因](../artifacts/iterations/2026-10-08-evaluation-http-real-runtime/development-log.md)：attempt-03因人为7168 MiB worker cap停止，末次采样7567 MiB、系统可用2642 MiB；实际瞬时峰值未知，不能归因为系统内存不足。源码未发现评测重复建模：共享lazy embedder和唯一shared reranker；CPU reranker显式float32，embedding dtype未钉死。跨lifespan synthetic恢复合同2项通过，真实API成功/取消/恢复未完成；本轮不重跑模型。

2026-10-08 [评测 attempt-04 阶段观测准备与启动前检查](../artifacts/iterations/2026-10-08-evaluation-http-real-runtime/development-log.md)：runner加入embedding/reranker首次调用、HTTP case/取消/shutdown/resume及checkpoint hash阶段标记；monitor加入9 GiB启动门槛和系统1.25 GiB安全线。单次preflight可用8735 MiB，低于门槛，未启动真实模型；本轮仅Python AST、PowerShell语法检查通过。

2026-10-08 [管理员评测GPU真实API单题生命周期](../artifacts/iterations/2026-10-08-evaluation-http-real-runtime/development-log.md)：修正GPU单行解析后，以真实CUDA模型经默认管理API完成固定单题成功、取消、跨lifespan shutdown/resume/checkpoint复用；独立测试Agent审读证据一致，Qdrant只读0写入。Python临时目录退出清理遇Windows SQLite WinError32，clean exit/自动清理未通过；非语义质量/生产验收。详见[独立审核](../artifacts/iterations/2026-10-08-evaluation-http-real-runtime/independent-review.md)。

2026-10-08 [管理调用日志SQLite连接释放修复](../artifacts/iterations/2026-10-08-evaluation-http-real-runtime/development-log.md)：`CallLogStore._connect()`改为事务型contextmanager并在finally关闭每条真实SQLite连接；旧码连接强引用合同4个handle仍可查询，修后同合同通过且Windows可重命名、删除临时root。attempt-07历史exit_code=1保持原样，不重跑模型；清理合同不替代真实runner新版本exit验证。




2026-10-14 [新版无状态MCP业务整合](../artifacts/iterations/2026-10-14-mcp-stateless-integration/development-log.md)：挑取隔离服务源码、独立uv依赖和部署配置至集成worktree，按当前模型准入、身份库与凭据能力适配。来源目录中的历史SDK/容器合同标为历史，不替代本轮兼容与独立验收。

