# MCP旧/新版入口管理前端

## 实现

- 增加管理员专用“MCP入口”导航页，展示固定旧版状态化（2025-11-25）与新版无状态（2026-07-28）入口卡：协议/传输类型、服务端端点、推荐默认标记、Docker运行标签及控制错误说明。原生进程明确为未受Docker管理，不能从此启停。
- 通过现有同源 `api()` 读取 `GET /api/admin/mcp/entries` 和对应 `connection-config`；只投影 `entry_id/name/protocol/transport/endpoint/auth.scheme/auth.credential_source/requirements`，不显示或复制任何额外响应字段。连接配置可在页面复制，未打开原始MCP接口。
- “设为默认”单独调用带 `expected_revision` 的 PUT。成功说明只改变未来推荐，不重配已有Agent、不启停服务；409会重读入口与revision并提示核对。
- Docker启动/停止按钮按控制配置、runtime_state、generation和单动作pending状态启用。start/stop都携带页面观察到的generation；stop有中断客户端连接的确认。202操作按ID轮询，呈现succeeded/failed/outcome_unknown；410 operation过期及网络不确定结果会触发entries刷新复核。entries刷新失败时暂时禁用对应入口控制，避免误报状态。
- 页面仅允许管理员账号会话访问，使用现有同源CSRF请求封装。账号切换/退出会清空入口状态、连接配置和操作revision；generation/response检查阻止迟到响应回填。错误与用户可见数据用安全DOM文本写入。所有状态说明明确：Docker容器运行不证明MCP协议或业务请求健康。

## 验证

- `node --check src/wenjin/file_management/admin/app.js` 与 `identity.js` 通过。
- `node --check tests/admin_frontend_catalog_refresh.test.mjs` 通过。
- `node --test tests/admin_frontend_catalog_refresh.test.mjs tests/admin_credential_ui_behavior.test.mjs tests/admin_identity_auth_behavior.test.mjs`：48/48通过。入口专项合同覆盖固定卡片与连接配置字段白名单、admin-only访问、未来默认项CAS/冲突刷新、无Docker副作用、native unmanaged禁控、start/stop generation、操作轮询、outcome_unknown/410过期后的入口复核、复核失败锁定及账号generation迟到响应。

未调用真实入口API、未改默认入口、未操作Docker/容器/凭据/授权、未重启服务。部署与浏览器验收由根Agent执行。
