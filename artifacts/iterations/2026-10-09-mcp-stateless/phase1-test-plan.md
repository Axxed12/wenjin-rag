# MCP Stateless 测试计划（2026-10-09）

## 当前边界

本轮测试只使用隔离合成数据和 loopback/TLS 代理。旧版 `2025-11-25` 入口继续由已有合同验证；新版 `2026-07-28` 必须由单独入口或明确的版本分流实现。无状态只描述 MCP 协议层，不代替个人凭据、资料库授权、管理站点登录会话或资料版本约束。

当前测试目录中的基线：

- `tests/test_mcp_identity_http.py`：旧协议的会话绑定、每次 bridge 请求读取授权、撤销 MCP credential 与 library grant。
- `tests/test_mcp_release_identity_tcp.py`：loopback TCP 下合成 release A/B、检索/取件、release 固定、错误 embedding model 和撤权行为。
- `tests/test_deployment_tls_proxy_acceptance.py`：隔离本地 TLS 代理、管理页 Cookie/CSRF 和旧协议 session。该文件不是新版协议验收。

上述测试提供可复用的业务合同，不能单独证明新版 MCP 能力。

## 第一批：协议、依赖和验收范围

### 证据与版本轴

- 规范版本：MCP `2026-07-28`。本文只引用定版规范目录，不把 `draft` 页面作为规范依据。
- 当前项目锁定值：`fastmcp==3.4.4`，`mcp==1.29.0`。协议版本、Python SDK 版本和服务框架版本属于不同轴。
- Python 官方 `mcp` 2.x 与 TypeScript 官方 `@modelcontextprotocol/client` 2.x 均有覆盖该协议的客户端/服务端实现。独立真实客户端选择 TypeScript Client；npm registry lookup on 2026-10-09 returned `2.3.1`, and the isolated acceptance package pins that exact version, not floating `latest`。
- 主客户端连接使用 `versionNegotiation.mode = { pin: '2026-07-28' }`。SDK 必须拒绝旧协议服务器，不允许自动退回 `initialize`。固定模式会以 `server/discover` 探测并拒绝降级；用 `getDiscoverResult()` 检查广告结果，再执行工具列表和工具调用。

### 协议合同矩阵

| 范围 | 输入/操作 | 预期 | 证据类型 |
|---|---|---|---|
| 发现 | 现代 SDK pinned 连接；读 discover 结果 | 只声明 `2026-07-28`；包含 `tools` capability 和服务身份元数据；无 initialize | 官方 TS SDK 真客户端 |
| 工具列表 | `listTools()` | 返回静态工具 schema；请求仍须有效凭据。若只含静态 schema，可用 public cache scope；结果有 `resultType=complete`、`ttlMs`、`cacheScope`；顺序稳定 | 官方 TS SDK 真客户端 + 收到的 wire |
| 工具调用 | 合成 query/file id 调用 search 工具 | `resultType=complete`，证据指向合成文档和目标 release；工具结果内容可被 SDK 解码 | 官方 TS SDK 真客户端 |
| 每请求元数据 | 对每个现代 POST 检查 body `_meta` | `protocolVersion=2026-07-28` 与 `clientCapabilities` 必须存在；`clientInfo` 可选且不用于授权 | 收到的 wire |
| 标准 HTTP headers | 检查每个现代 POST | `MCP-Protocol-Version` 与 `_meta` 相符；`Mcp-Method` 与 body method 相符；`tools/call` 的 `Mcp-Name` 与 params.name 相符 | 收到的 wire |
| header 校验负例 | 缺 header、header/body mismatch、缺 required `_meta`、不支持版、未知 method | 依定版规范返回 HTTP/JSON-RPC 状态与代码；现代流量不进入旧版 handler | 定向合成负例（不能替代 SDK 正例） |
| 错误结果 | 工具业务错误、无权限、失效 release | 区分 MCP transport/protocol error 与合法的 tool result；客户端看到错误语义稳定且不泄漏 credential 或内部路径 | 官方 TS SDK + 合成夹具 |
| 凭据/ACL | 未授权、有效授权、撤 MCP credential、撤 library grant | 每个现代请求（包括 tools/list）都校验 token；静态工具 schema 不按个人资料权限变形。用户资料列表/检索/下载读取当前 grant；撤权后同一连接/新请求均拒绝或返回既定的不可用语义 | 官方 TS SDK + 合成 identity store |
| 版本绑定 | release A 查询后切换 active 到 B，再请求 A 的下载；传不存在/错模型 release | 下载路径仍绑定 A；未知/不匹配版本 fail-closed；不能静默切到 active B | 官方 TS SDK + 两版合成 release |
| 新旧隔离 | 新版 pinned client 请求旧入口；旧版客户端请求旧入口；分别请求新版 route | 新版对旧入口失败且不产生旧 session；旧入口仍按 `2025-11-25`工作；新旧 route 的工具/auth/log/state 不串线 | SDK clients + 两个独立 route |
| session header兼容 | 现代请求带任意、包括旧版曾绑定的 `Mcp-Session-Id` | 新版忽略该头；仍独立认证请求主体；不触发旧版 session-user 绑定或复用跨账号状态；不mint/echo session id | 定向 HTTP 兼容负例 + SDK正例 |
| 现代 endpoint 方法 | 对现代 Streamable HTTP endpoint 发 GET/DELETE | HTTP 405；不回退到旧版 SSE/session lifecycle | 定向负例 |
| TLS 代理 | TLS CA 验证；modern SDK discover/list/call | 代理保留必要 MCP headers 和 Authorization；响应语义完整；管理 Cookie 仍 Secure/HttpOnly，CSRF 仍有效 | 官方 TS SDK + 隔离本地 TLS proxy |

### 关键失败语义

- 缺少现代请求必需 `_meta`：HTTP 400、JSON-RPC `-32602`。
- 缺/错配 `MCP-Protocol-Version`、`Mcp-Method`、必需的 `Mcp-Name`：HTTP 400、JSON-RPC `-32020`（HeaderMismatch）。
- 不支持现代协议版本：HTTP 400、JSON-RPC `-32022`（UnsupportedProtocolVersion）。
- 未实现方法：HTTP 404、JSON-RPC `-32601`。
- 现代协议 endpoint 的 GET/DELETE：HTTP 405。若请求带旧版 `Mcp-Session-Id`，现代路径忽略该头，不 mint 或 echo 会话头。
- 授权失败属于应用/HTTP 身份语义，按项目既有 bearer middleware 合同验证；不混成 JSON-RPC 协议错误。

## 后续批次门槛

1. 协议入口由开发 owner 的 framework/API probe 明确后，再写针对真实入口的合同测试；在 probe 前不假定一个框架能在同一 app 同时提供两个协议时代。
2. SDK 客户端正例必须由固定的官方 TypeScript Client 包执行；普通 HTTP 负向测试只用于检查错误格，不作为客户端实现证据。
3. 新版权限、撤权和版本绑定测试只使用 `tmp_path` 下生成的 identity DB、合成索引和合成资料。禁止连接现有 Qdrant、真实 DB、账户或真实语料。
4. TLS 联验只用本地临时 CA 和本地代理。若代理没有实际启动，记录为“尚未验证”，不能由 Python 直连测试替代。
5. 不运行冻结评测集，不重跑与此协议合同无关的历史测试。

## 参考资料

- [MCP 2026-07-28 基础规范](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/index.mdx)
- [MCP 2026-07-28 Tools](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/tools.mdx)
- [MCP 2026-07-28 Discovery](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/discover.mdx)
- [MCP 2026-07-28 Changelog](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/changelog.mdx)（required HTTP routing headers、cache fields 与最终错误码）
- [TypeScript SDK v2 协议版本指南](https://github.com/modelcontextprotocol/typescript-sdk/blob/main/docs/protocol-versions.md)
- [TypeScript SDK Client package](https://github.com/modelcontextprotocol/typescript-sdk/tree/main/packages/client)
- [Python SDK releases](https://github.com/modelcontextprotocol/python-sdk/releases)
- Independent real-client harness: `tests/acceptance/mcp-stateless-client/` (`@modelcontextprotocol/client==2.3.1`)
