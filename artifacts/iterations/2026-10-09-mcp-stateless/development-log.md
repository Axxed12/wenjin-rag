# MCP 2026-07-28 无状态协议个人迭代

## 2026-10-09 接手、隔离与第一批设计

问题：聊天worktree位于旧HEAD，缺少当前未提交的管理/身份/部署模块；E盘原开发目录大量未提交修改。原因：这些功能尚未提交到原Git HEAD。处理：只读复制代码/文档到独立worktree、建立基线提交842f1a2，再开发。证据：[快照hash](handoff-snapshot.json)。不切换/reset/stash原目录，不操作原服务、数据、凭据、索引或数据库。

问题：现有协议限制要求2025-11-25，主lock为FastMCP3.4.4/SDK1.29.0。处理：按用户最新授权将本轮明确归于截止日后的个人迭代，旧入口规则继续保留；评估独立FastMCP4.1.0/SDK2.3.0环境和服务，不在主环境升级。依据、改动范围与验收顺序见[第一批计划](phase-1-plan.md)。两名GPT-6 Luna/high子Agent已实际启动，开发/测试文件分开。

检索过程：agent-reach Exa后端报告本机证书错误，gh只读API因未登录拒绝；切换可用web工具直接读取官方规范/SDK/框架文档，未修改证书信任或登录配置。agent-reach check-update显示1.5.0为当前版本。

状态：第一批官方核验与方案已记录；依赖/API实际probe和独立客户端registry证据仍在进行。第二至第四批尚未验收；真实模型、真实语料、冻结集、WAN与生产验收未执行。

## 第一批依赖与客户端实测检查点

开发owner实际运行resolver：FastMCP3.4.4 + mcp2.3.0不可解（fastmcp-slim3.4.4要求mcp<2）；独立FastMCP4.1.0 + mcp2.3.0可解并安装。Context、get_http_request/get_http_headers仍可导入；HTTP装配API由streamable_http_app转为http_app。证据见[依赖probe](dependency-evidence/fastmcp-sdk-compatibility.md)。原主pyproject/uv.lock和原环境未升级。框架工具decorator/Context运行适配probe通过后方可进入最小实现。

独立测试owner从npm registry核验并实际安装官方TypeScript Client2.3.1，固定modern模式脚本语法及公开Client API核对通过，尚未连接服务。见[独立测试计划](phase1-test-plan.md)和[测试记录](test-development-log.md)。基础Python Client固定SDK2.3.0，新旧协议/SDK/框架版本分别记录。

第一批门槛完成：隔离FastMCP4实际调用既有register_tools成功注册search_docs、list_corpora、list_managed_files三项，未调用业务或连接任何数据服务。这证明装饰器/helper注册API可复用，不是网络协议验收。root在第一批计划/风险/矩阵交付后授权开发owner进入第二批；下一步专门适配现代Context/协议标签与独立HTTP入口。

审核修正：modern-only HTTP对旧Mcp-Session-Id/Last-Event-ID按规范忽略，不用于身份或会话绑定、不生成/回显session；GET/DELETE返回405。静态工具schema可统一公开，资料目录与工具调用/下载仍逐请求授权，不能从工具schema缓存推断资料权限。

## 第二批网络检查点

独立测试owner已产生[官方TypeScript SDK网络证据](sdk2-network-phase2.json)：Client2.3.1固定modern，真实loopback TCP上的server/discover、tools/list、list_corpora工具调用各返回HTTP200；supportedVersions仅2026-07-28，三个请求均含协议meta/能力与标准HTTP头，不发送initialize/session头；三个resultType均complete，身份位于result._meta。discover/list由SDK返回ttlMs=0/cacheScope=private，call返回no-store、isError=false且structuredContent协议标签2026-07-28。调用现有list_corpora处理器返回合成空目录，不是检索质量、模型或真实语料验收。

实现审核处理：独立协议入口/注册facade已新增；纠正业务标签与isError、专属下载前缀、Context内部UUID不冒充wire session、JSON-RPC错误id、4MiB请求上限和后续disconnect转交。生产launcher只读取显式配置，不initialize身份库；guard要求独立SDK2/框架4环境和明确个人/静态bearer认证，非loopback需TLS或明确私网代理opt-in。旧server/tools/core与主依赖保持基线。

仍待独立协议负例、完整账号/授权/版本链及容器/TLS验收。正式Docker overlay为remote模型轻量服务，未启动原stack。

## 第三批身份、检索与版本绑定检查点

独立测试owner以临时IdentityStore、两个用户、真实管理HTTP/ManagedFileHTTPClient、进程内Qdrant及确定性marker向量器运行实际TypeScript SDK2.3.1网络调用。[结果](sdk2-identity-release-phase3.json)记录：Alice授权可列出文件，Bob即使发送同一个旧session头也为空；bridge各请求携带当前个人身份。共享release A搜索成功，切active至B后，A下载URL仍含release_id=A并返回A的精确字节/hash；B检索绑定B。未知release取件403、错误版本为工具错误。受管文件v1取件200，替换后旧URL409、新v2取件200且含新marker。撤grant后目录为空，撤credential后discover401。该合同单独通过；重排器关闭，不代表模型/质量验收。

新发现：增加非字符串protocolVersion=123负例后，FastMCP4/SDK2默认先做header比对并返回-32020，规范要求Malformed meta为-32602。处理：新版入口仅对已存在且非string的该meta键做局部校验，保留request id并返回HTTP400/-32602，其余协议仍由官方SDK/框架处理。该适配明确记为项目修复，不能据此声称SDK自身所有负例完全符合规范。只复验变更的协议合同，不重复已通过的完整身份/版本链。

第四批镜像构建已授权；测试owner是唯一临时Docker/TLS stack操作者。待实际新旧入口隔离、只读identity WAL读行为、TLS SDK调用与清理证据后再完成验收。正式overlay只给MCP模型secret，file_admin_token仍仅给管理服务。

协议类型修复复验通过，定向输出为[phase2-3-targeted-tests.txt](phase2-3-targeted-tests.txt)：6 passed、6 subtests passed。测试owner实际运行时与身份合同合并了一次；后续不再重复已通过业务链。

新旧入口实测完成：[protocol-era-isolation.json](protocol-era-isolation.json)。原环境SDK1.29.0只读运行临时旧入口，协商2025-11-25、生成session并完成工具列表/调用；官方TS SDK2.3.1固定modern连旧入口只发送server/discover后失败，没有initialize降级；SDK1连新版初始化被拒且无session。两个进程只读取独立worktree源码和临时合成资产，未接触原运行服务。

收尾快照审核发现：原仓库通用`models/`忽略规则也匹配src/wenjin/models，因此已复制的既有remote provider源码没有进入初始Git基线。补充提交91dd611只保存这两个原快照模型模块和三份被忽略的参考文档，内容与handoff hash一致；它们是此前已存在的接手资产，不是本轮新增的模型能力。没有修改原仓库忽略规则或原文件。

## 第四批 WAL 失败与处理决策

实际失败：正式镜像e2cf7cea…b2b3在临时IdentityStore默认WAL模式、writer连接全部关闭且sidecars不存在时，身份目录filesystem只读挂载导致HTTPS发现HTTP500，SQLite报`unable to open database file`。host writer更改grant并关闭后，下一SDK请求仍500；数据正文在无writer读段未改变。该失败不能用切换journal_mode=DELETE或保持人工writer连接掩盖。

处理决策：仅新版新增`query_only`身份读取连接，禁止应用账户/授权写操作；仅身份卷允许SQLite维护WAL/SHM sidecars，语料和索引挂载继续只读。不initialize、不改schema/journal，不修改旧IdentityStore。待独立验证无sidecars首次鉴权、reader grant/revoke即时可见、write API拒绝及无writer读段正文hash不变后才能通过第四批。临时Caddy重建应用后缓存了旧IP引起502，测试环境需重启代理/稳定别名；该502不属于协议实现成功证据。

## 第四批最终联验与交付

最终镜像`sha256:1f00cf6f96f22b68302845bad5eb8dbd2e44e976c720edf5029d9849d709fa62`固定运行正式CLI，独立测试owner使用临时Qdrant1.19.1、合成索引/原文、临时reader账户/授权、合成remote embedding/rerank API及真实Caddy HTTPS执行官方TS Client2.3.1。最终合同1 passed（108.93s）；[实际输入输出](docker-launcher-tls-runtime.json)包括discover/list/search成功、取件200且精确字节/hash/no-store、撤grant后目录空/旧URL403、重授后即时恢复、query_only拒写和各无writer读段DB正文hash不变。首次鉴权保持WAL且没有预制sidecars，未切journal mode。

管理fallback运行真实create_app账户模式，通过TLS实测登录200、Secure/HttpOnly Cookie、错误Origin403、缺CSRF403、正确CSRF签发201。旧/mcp与/files经合成upstream转发用于代理路径验收；旧SDK的初始化/session/list/call及入口互拒已由第三批独立TCP验证，不能把代理探针表述成旧协议TLS全栈联验。正式容器样例使用index-registry取件，不声称URL带release_id；immutable发布绑定按第三批release-catalog实际证据。

本阶段问题/修复均记录：测试fixture旧Starlette middleware装饰器、临时network静态IP冲突、management测试server未信任Caddy转发IP，分别改为BaseHTTPMiddleware、空闲地址、精确forwarded_allow_ips；最终按同一合同复验。生产源码未因此扩展。协议fail-closed新增后定向6 passed/6 subtests，旧SDK收集新版模块明确skip，定向Ruff通过；未运行冻结或无关历史测试。

收尾审核又发现两处摘要被手工转录多了字符。开发/测试owner均改用动态SHA计算；独立测试owner用无挂载短命容器逐个核验最终镜像内三份新版源码和manifest/lock，全部匹配当前工作树。宿主部署模板hash与镜像文件hash分开记录，没有为文字纠错重建或重跑业务链。

四批实现与合成验收完成，见[交付汇总](acceptance-summary.md)和[操作说明](../../../docs/mcp-stateless.md)。原目录文件hash、旧核心/依赖/部署diff及新增文档链接按[审核结果](root-review-checks.json)收尾。没有真实模型/语料、冻结评测、WAN/GUI/生产验收，没有合入原目录或部署现有stack。最终容器/网络/工作目录已清理；早期三处临时SQLite目录删除被自动审查拒绝，理由仅`blocked by policy`，未绕过，剩余清单见[清理记录](cleanup-evidence.md)。
