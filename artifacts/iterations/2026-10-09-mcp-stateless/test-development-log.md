# 测试开发记录

## 2026-10-09：第一批核验与验收范围

- 问题：项目已有 MCP identity/release/TLS 合同使用 `2025-11-25` initialize/session，无法证明 `2026-07-28` 协议行为。
- 原因：当前锁文件仍为 `mcp==1.29.0`，主依赖为 `fastmcp==3.4.4`；已有 loopback/TLS合同均显式发起 initialize 并检查 `Mcp-Session-Id`。
- 处理：新增 phase 1 验收矩阵，将官方协议合同、SDK 网络正例、授权撤销、release 绑定、新旧隔离和 TLS 分阶段；独立客户端选 TypeScript 官方 SDK，固定现代协议，不使用 auto fallback。
- 证据：官方规范要求每请求 `_meta` 中协议版本和 client capabilities；HTTP 标准头 `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` 必需并与 body 核对。规范错误格包括 `-32020` / `-32021` / `-32022`；工具结果要求 `resultType`，可缓存列表结果带 `ttlMs` / `cacheScope`。
- 验证边界：本条为官方文档核对和既有合同静态检查；未启动服务、未运行测试、未调用模型、未联验容器/TLS、未作真实客户端连接。
- 未解决：等待开发 owner 确认 FastMCP/SDK 入口 API 与 dual-era 路由方案后，再写针对新版入口的 pytest 合同。真实客户端依赖环境正在独立准备。

## 2026-10-09：独立真实 SDK 客户端准备

- 问题：需要用真正 MCP SDK Client 验收现代发现、tools/list 和 tools/call，同时保证客户端不因发现失败回退到旧协议。
- 处理：在 `tests/acceptance/mcp-stateless-client/` 建立隔离 Node package，固定 `@modelcontextprotocol/client==2.3.1`；连接选项 pin `2026-07-28`，捕获 SDK 发出的脱敏 wire 摘要，验证现代发现、工具列表与调用、必要头、逐请求 meta、Bearer 存在、没有 `initialize` / `Mcp-Session-Id`。
- 环境证据：Node `v24.14.0`、npm `11.9.0`；`npm view @modelcontextprotocol/client version` 返回 `2.3.1`；隔离 `npm ci` 安装 13 packages；`npm ls --depth=0` 显示精确 `2.3.1`；`node --check client.mjs` 通过。
- 验证边界：只验证客户端依赖已安装和脚本语法；没有启动服务、没有联网调用 MCP endpoint、没有完成发现/list/call，也没有测试真实模型、容器或 TLS。
- 未解决：等待开发 owner 给出新版入口命令与合成运行配置后，使用临时 FastMCP 4.1.0 / mcp 2.3.0 环境运行 SDK client，并加入权限、撤权和 release 绑定合同。
- 后续边界已明确：现代请求若携带旧版 `Mcp-Session-Id`，新版应忽略该头，不 mint/echo 新 session，也不得用旧 session binding 选择主体；现代 endpoint 的 GET/DELETE 应为 405。静态工具 schema 可以统一公开并使用 public cache scope，但每次 `tools/list` 仍做 credential 校验；资料 ACL 在资料列表、检索、下载处逐请求判定。
- 依赖版本补充：GitHub Releases 页面最上方显示 Python SDK 2.2.0，但 [PyPI 已发布的 2.3.0 wheel/sdist](https://pypi.org/project/mcp/2.3.0/) 上传时间为 2026-10-02；开发隔离环境使用 2.3.0 与该包版本相符。
- root静态审核后：响应摘要中的 `serverInfo` 按定版规范从每个 response result 的 `_meta` 读取；列表缓存范围允许 `public` 或 `private`，不把 public 当协议硬要求。正向 search client合同增加 `isError=false`、wire `resultType=complete` 和 structuredContent顶层/嵌套 `mcp_protocol=2026-07-28`。
- root确认不为tools/call增加CacheableResult字段：资料检索响应使用HTTP `Cache-Control: no-store`政策并由客户端证据检查；错误合同增加response id与请求id对应检查，meta字段缺失或类型错为HTTP 400 + `-32602`；unsupported-version负例头/body协议值一致后才期望 `-32022`。

## 2026-10-09：第二批现代发现、列表和调用

- 输入：官方 `@modelcontextprotocol/client==2.3.1` 固定 `versionNegotiation.mode.pin=2026-07-28`；服务通过 FastMCP 4.1.0 / Python SDK 2.3.0 isolated env 在随机 loopback 端口启动。临时项目根、合成 RuntimeIdentity、空 AccessRegistry、临时审计流和本地合成 Bearer key；不读取现有 `.env`、索引、数据库、账户或语料。
- SDK 实际输出：发现 `supportedVersions=["2026-07-28"]` 并声明 `tools` capability；`tools/list` 返回 `search_docs`、`list_corpora`、`list_managed_files`；调用 `list_corpora({})` 返回 `isError=false`、`resultType=complete`、`structuredContent.mcp_protocol="2026-07-28"`。列表结果带 `ttlMs=0`、`cacheScope=private`，每个响应有 `serverInfo`。
- Wire证据：`sdk2-network-phase2.json` 捕获三次POST（`server/discover`、`tools/list`、`tools/call`）；每次都有协议头和 `_meta` 版本、clientCapabilities、Bearer，`Mcp-Method` 与body相符；call带 `Mcp-Name=list_corpora`。没有 `initialize` 或 `Mcp-Session-Id`。个人调用响应 `Cache-Control=no-store`。证据不保存token和工具参数。
- 定向负例：`tests/test_mcp_stateless_protocol.py` 使用与production相同的 FastMCP4 app factory / middleware；缺失或错误meta、缺少/不匹配路由header、头/body版本不一致返回HTTP 400和预期错误码，且错误JSON-RPC id等于请求id；头/body都为旧版本时返回 `-32022`，未知方法返回404/`-32601`，GET/DELETE返回405。任意旧 `Mcp-Session-Id` 不会 mint/echo session；没有Bearer时仍为401。
- 验证：`protocol-wire-tests.txt`；隔离 FastMCP4.1.0 / mcp2.3.0 环境中运行 `python -m pytest -q tests/test_mcp_stateless_protocol.py`，结果 `5 passed, 5 subtests passed`。pytest仅安装在该 `%TEMP%`隔离环境。
- 证据边界：这是合成service的真实网络 TypeScript SDK调用，加上同一ASGI app的协议错误合同；没有真实模型调用或真实语料检索。成功业务调用目前是空合成目录的 `list_corpora`，尚未证明个人凭据/授权撤销、release版本下载、容器/HTTPS代理或其他桌面客户端。
- 未解决：第三批需使用隔离身份库、两用户凭据/授权、合成检索与两个release实测跨用户session头隔离、撤权、release固定和错误版本取件；第四批需在无既有资产挂载的临时Docker/TLS环境运行同一 pinned SDK客户端，并验证只读身份卷的真实SQLite WAL读取行为。

## 2026-10-09：第三批身份、授权与版本绑定（首轮）

- 夹具：temporary project root + 合成两版 shared release fixture + 进程内 Qdrant + 合成 managed file；临时真实 IdentityStore 建立 Alice/Bob 两个账户和 MCP credentials。文件管理服务由随机 loopback Uvicorn 提供真实 HTTP，MCP account bridge 使用 `ManagedFileHTTPClient(account_mode=True)`，MCP 新入口也由 loopback Uvicorn 提供。仅 Node 子进程环境注入一次性 synthetic tokens，不继承其余主进程环境变量。
- 正向输入输出：官方 TS Client 2.3.1 发 `search_docs` 查询 shared release A 和 managed V1/V2 合成 marker。shared release A 的返回下载地址含不可变 A release_id；切换active到B后 GET 该URL仍返回A原文，SHA-256与合成A来源完全一致；随后搜索命中B并返回 release_id=B。managed V1被替换后旧version_id下载返回409；V2查询和取件返回V2 marker。MCP 返回 `mcp_protocol=2026-07-28` 和成功结果 `isError=false`。
- 身份权限：官方客户端对Alice/Bob都发送相同旧 `Mcp-Session-Id`；Alice只看到已授权文件，Bob列表为空，SDK收到的响应没有session id；management bridge捕获到的每个token都对应当前请求用户。撤Alice library grant后下一次工具列表变空；撤Alice MCP credential后 discover返回401。错误shared version由SDK得到 `isError=true`；未知release取件以403隐藏拒绝。
- 证据：`sdk2-identity-release-phase3.json` 包含无凭据的版本/状态/hash/bridge身份摘要。`tests/test_mcp_stateless_identity_release.py` 首轮单独通过 `1 passed`。
- 问题：与协议矩阵合并重跑时，新加的 `protocolVersion=123` 负例收到HTTP 400 / `-32020`，而根级合同要求类型错误返回 `-32602`。具体重现请求在 `tests/test_mcp_stateless_protocol.py` request id 604；已交给开发 owner修正入口gate，待修复后重跑定向矩阵。
- 验证边界：shared/managed检索均使用确定性合成向量和禁用的 reranker；没有真实模型/语料调用。当前MCP网络路径由官方TypeScript SDK实测；HTTP GET只用于取件本身，并校验版本绑定与正文SHA-256。
- 未解决：等待 `protocolVersion` 类型校验改为 `-32602` 并重新跑 targeted tests；第四批仍需无真实资产挂载的容器/HTTPS代理与只读身份卷/WAL验证。

## 2026-10-09：第三批与协议矩阵复验

- 处理：开发 owner为显式非字符串 `_meta.protocolVersion` 增加 `InvalidParams (-32602)` gate并保留请求id；没有改RAG核心或放宽协议负例。
- 复验：FastMCP4.1.0 / Python SDK2.3.0 isolated env运行 `python -m pytest -q tests/test_mcp_stateless_protocol.py tests/test_mcp_stateless_identity_release.py`，结果 `6 passed, 6 subtests passed`；包含protocolVersion数字、缺失/错误capabilities、headers匹配、unsupported version、未知方法、GET/DELETE、旧session header、完整Alice/Bob管理端与release合同。输出为 `phase2-3-targeted-tests.txt`。
- 实测业务证据：正式TypeScript SDK 2.3.1对shared A/managed V1/V2执行 `search_docs`，返回marker和modern structuredContent；shared A下载路径绑定A release id，active切B后仍下载A且SHA-256一致；managed V1旧下载在替换后409，V2按新版本取件。撤library grant后列表即时清空，撤credential后discover 401。对应脱敏结果为 `sdk2-identity-release-phase3.json`。
- 当前未解决：第四批Docker/HTTPS proxy/真实只读identity卷 WAL实际请求仍未验收；真实模型、真实语料、外部桌面客户端仍未验证。

## 2026-10-09：第四批正式 launcher、TLS、WAL 与管理会话

- 最终运行对象：正式 `wenjin-mcp-stateless:2026-10-09` image `sha256:1f00cf6f96f22b68302845bad5eb8dbd2e44e976c720edf5029d9849d709fa62`；CLI为 `python -m wenjin.mcp.stateless_server --host 0.0.0.0 --port 18768 --identity-db /var/lib/wenjin/identity.sqlite --project-root /app/project`。镜像源码/配置 SHA-256 记录在 `docker-launcher-tls-runtime.json` 和开发 owner 的 build-log；测试没有挂载当前仓库 `src` 覆盖镜像源码。
- 隔离输入：一次性Docker bridge network、127.0.0.1随机映射端口、Qdrant `v1.19.1`无卷容器、fixture生成的OSG 1.0/2.0索引与原文、独立临时WAL身份库/reader grant、确定性合成remote模型API。identity目录只为SQLite sidecar协调以RW挂载；主库 `journal_mode=WAL`，初次鉴权之前确认 `-wal/-shm` 均不存在。没有使用现有服务、语料、索引、账户、授权或数据库。
- 官方网络客户端：`@modelcontextprotocol/client 2.3.1` pin `2026-07-28`，经临时Caddy internal CA HTTPS连接生产 `/mcp-stateless` 路由，完整执行discover、tools/list、search_docs。检索得到 `release_marker_A`，`resultType=complete`、`isError=false`、structured protocol标签正确。模型API观测到 identity、query embedding、rerank 请求；这是合成模型适配器验证，不是真实模型调用。
- 取件：SDK搜索返回的`evidence[0].download_path`经同一个TLS代理GET；状态200、HTTP `Cache-Control=no-store`，77字节SHA-256 `b8e514ed7af6f7f24765ebc736481159ef2f8d1a8a5a9205aa5d68c9bc057707`与合成fixture原文一致。该正式launcher样例使用index-registry target，没有声称其URL包含immutable release_id；immutable shared-release A/B绑定已由第三批release-catalog合同证明。
- 身份与WAL：`StatelessIdentityStore`首次无sidecar HTTP鉴权成功；query_only=1、`initialize()`被拒、SQL UPDATE被SQLite拒绝。首次SDK读与query-only探针后主DB哈希不变。host writer撤掉reader grant后，紧接着SDK `list_corpora`返回实际空数组、旧取件URL为403；writer重新授予后下一次SDK列表恢复为单库、旧URL恢复200。每个writer后的无写请求区间均比较主库SHA并保持不变；各段基准哈希在JSON证据中分别列出。
- 代理与管理会话：Caddy配置从 `deploy/Caddyfile.stateless` 读取，仅将域名改为localhost、加入`tls internal`、将合成legacy upstream端口改为8000；实际 `/mcp-stateless`、`/mcp`、`/files/*`和fallback matcher/顺序来自部署文件。`/mcp`与`/files/*`到合成legacy upstream的转发探针通过；旧版SDK1.29 TCP初始化/session/list隔离已由`protocol-era-isolation.json`单独证明，这里未声称通过TLS完成legacy MCP握手。management fallback运行正式`create_app`账户模式与独立临时IdentityStore，通过TLS实测登录200、cookie `Secure`/`HttpOnly`并被客户端保存、错误Origin 403、缺CSRF写请求403、正确CSRF签发个人凭据201。
- 验证结果：最终定向命令 `MCP_STATELESS_CONTAINER_ACCEPTANCE=1 python -m pytest -q tests/test_mcp_stateless_launcher_runtime.py -s`，结果`1 passed`；证据为`docker-launcher-tls-runtime.json`。测试期间三轮可修复问题分别是测试model app使用已移除的Starlette middleware decorator、静态IP选择冲突、management测试server未信任临时Caddy转发IP；修复均限于测试夹具/地址参数，最终完整链通过。早期旧只读WAL失败保留在`docker-tls-wal-readonly-failure.json`，没有重跑或覆盖。
- 清理：本轮成功移除所有`mcp-stateless-*`临时容器/网络与`mcp-stateless-final-*`临时工作目录；历史三处受策略拒绝删除的临时目录仍按`cleanup-evidence.md`列示。本轮没有新增残留容器、网络或测试工作目录。
- 最终镜像绑定复核：审核发现早期证据中两个手工转录SHA有误；重新从工作树动态计算host template SHA，并用无挂载、`--rm`、只读Python hash命令读取最终镜像中的三个`/app/src/wenjin/mcp/stateless_*.py`文件及`/app/mcp-stateless/{pyproject.toml,uv.lock}`。五项镜像内SHA全部与当前源码一致；修正后的逐项结果及image ID记录于`docker-launcher-tls-runtime.json`。Dockerfile、compose和Caddyfile SHA单独标为host template hash，不声称它们位于镜像内；未重建镜像或重跑业务链。
- 未验证边界：未调用真实模型或真实语料；未运行冻结评测；没有其他桌面/IDE客户端参与联验；旧入口TLS完整MCP握手未在本批重复；本批Caddy管理会话用synthetic管理员和文件管理后端，不代表真实账户/真实业务数据验收。
