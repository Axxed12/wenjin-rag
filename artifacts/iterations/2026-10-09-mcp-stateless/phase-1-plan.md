# 第一批：新版无状态 MCP 核验与实施边界

日期：2026-10-09。本轮是2026-07-15后的个人迭代；下列方案不属于此前项目能力。方案与测试由root审核，开发与独立测试分别使用用户指定的GPT-6 Luna/high。

## 接手与隔离

已读取AGENTS.md、PROJECT.md、code.md、docs/dual-deployment.md及2026-10-08双部署开发/测试记录。E盘原目录存在大量未提交源码与未跟踪模块；直接从HEAD开发会遗漏身份、管理桥、发布与部署功能。复用本聊天独立worktree `C:/Users/Onebiter/.codex/worktrees/2a22/rag-project`，分支`codex/mcp-stateless-20261009`。从原目录只读复制源码/配置模板/文档快照，基线提交`842f1a2`；[逐文件hash清单](handoff-snapshot.json)记录实际复制范围。未复制.env、secret、真实原文、模型、数据库和运行索引目录。没有stash、reset或切换原目录分支。

## 三个版本维度与官方依据

| 维度 | 旧入口 | 新入口候选 |
|---|---|---|
| MCP协议日期 | 2025-11-25 | 2026-07-28 |
| 官方Python SDK包mcp | 当前lock 1.29.0 | 精确固定2.3.0，须独立resolver及运行核验 |
| 应用框架FastMCP | 当前3.4.4 | 独立4.1.0环境；不升级主环境 |

规范日期决定wire行为；SDK/框架版本是实现依赖，不能互相替代。`stateless_http=True`只描述旧传输设置，不能证明新版规范实现。

官方依据：[版本与双栈规则](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)、[逐请求元数据和结果](https://modelcontextprotocol.io/specification/2026-07-28/basic)、[HTTP binding](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)、[发现](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)、[工具](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)、[Python SDK发布](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.3.0)、[Python Client版本模式](https://py.sdk.modelcontextprotocol.io/protocol-versions/)、[FastMCP发布记录](https://gofastmcp.com/updates)。官方SDK和FastMCP4提供两时代共存能力；现有FastMCP3/SDK1不能在同一依赖环境直接接入SDK2。实际resolver/API结果须附在本轮证据目录，文档声明不替代运行证明。

## 可复用与改动范围

复用`wenjin.mcp.tools.register_tools`中的工具业务逻辑、runtime检索与多库路由、SearchPolicy/重排器、RequestReleaseResolver、来源/hash校验、managed HTTP桥、IdentityStore逐请求个人credential和grant校验，以及现有下载实现。管理页面登录session仍由管理HTTP维护。

优先在独立FastMCP4进程装配新入口，保留原主pyproject/uv.lock与旧server/tools不变。新入口候选`/mcp-stateless`、独立端口18768；路径/端口只属模板，不启动现有stack。新进程使用同一受限管理HTTP桥与明确只读资产挂载；不另起管理数据库writer，不复制账户授权。若既有Context/装饰器API不兼容，只增加薄适配模块，不重写RAG核心；影响旧入口的抽离必须另行审核。

文件归属：开发owner负责新增`src/wenjin/mcp/stateless*.py`、独立`mcp-stateless/`依赖manifest/lock及`deploy/*stateless*`。测试owner仅负责`tests/test_mcp_stateless*.py`、`tests/acceptance/*stateless*`及测试证据/记录。root负责AGENTS/PROJECT/code/说明文档、开发记录索引和汇总审核。同一文件不得双写。

## 客户端验收选择

基础客户端为官方Python SDK2.3.0网络Client，`mode="2026-07-28"`固定新版并显式发现，不接受auto降级。独立测试使用官方TypeScript SDK客户端`@modelcontextprotocol/client==2.3.1`（本轮npm registry实际返回并安装），使用pin模式；发现→工具列表→工具调用经真实SDK transport执行。普通HTTP只用于非法wire/错误响应探针。Claude Desktop、Codex GUI和Inspector未证实支持本版，不算本轮已验收客户端。独立细项见[测试计划](phase1-test-plan.md)。

## 分批验收清单

1. 第一批：实际依赖解析（旧组合冲突/新组合可解），Context与装饰器兼容probe，官方固定规范与客户端包可用性；交付本计划、风险和独立测试矩阵后才开始最小实现。
2. 第二批：真实loopback TCP + 固定现代SDK，显式`server/discover`、`tools/list`、一次真实工具handler调用；捕获请求meta/headers及响应，不出现initialize/initialized/session header。列表与结果中的`resultType`、cache hints、serverInfo/capabilities必须按规范核验。
3. 第三批：临时身份/合成两版资料与进程内Qdrant，真实管理HTTP桥+MCP TCP；验证两用户隔离、每请求credential/grant、撤credential/撤grant即时生效、检索与来源绑定取件、错误版本及发布切换。旧协议入口继续initialize/session，新路径拒绝旧版，新SDK pin旧路径失败，跨入口不串线。
4. 第四批：独立Docker服务与本机自签TLS代理（校验CA）；独立TS客户端发现/list/call，代理完整转发协议头与Authorization；补启动、并存、切换、回退说明及资源清理证据。正式模板和合成容器联验分开记录。

必要错误合同：缺必需meta→HTTP400/-32602；缺/不匹配协议/方法/名称头→HTTP400/-32020；不支持协议→HTTP400/-32022及supported/requested；未知method→HTTP404/-32601。授权拒绝不能被客户端可伪造clientInfo或工具参数身份绕过。工具业务失败与JSON-RPC协议错误分别核验。

## 风险与停止条件

- FastMCP4 Context/HTTP helper或旧decorator不兼容：优先薄适配；不能用手写HTTP替代SDK。
- 旧业务返回`mcp_protocol=2025-11-25`：新版适配层须给出准确身份，不修改历史旧入口语义；audit/session访问需现代兼容。
- cache hints不能缓存授权判断；个性化结果使用private策略，撤权在每次call和取件再查。列表工具schema与资料内容目录是不同对象。
- 新进程不得触发真实索引准入/构建、账户初始化或原stack重启；测试仅临时资产，启动默认需显式配置。
- Docker配置渲染、镜像构建、合成容器/TLS联验、真实模型与真实客户端产品验收分别记账。真实模型/语料、WAN/云端、冻结评测均不在本轮执行范围。
- 指定子Agent模型已成功启动；若后续模型不可用报告阻塞，不自行替换。

未完成状态以[开发记录](development-log.md)和[独立测试记录](test-development-log.md)为准。
