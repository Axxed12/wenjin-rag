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
