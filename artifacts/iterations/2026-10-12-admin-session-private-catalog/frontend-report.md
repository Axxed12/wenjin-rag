# 管理界面账户入口与私域目录状态（前端）

## 实现

- 管理页顶栏新增可键盘操作的账户与连接菜单。账户模式显示登录名和管理员/只读身份，并提供“切换账号”“退出登录”；旧版模式标为 Bearer 管理连接或本机管理服务，提供连接设置与断开连接。
- 退出先发出携带当前 CSRF 的登出请求，再立即清除身份和页面敏感数据、推进身份代次；慢响应被旧代次校验丢弃。服务器请求结束后才打开新登录流程。没有新增凭据持久化或 URL 传递。
- 身份页面说明 MCP 凭据绑定使用账号，用户可自行签发，管理员可为指定账号签发，不应共享管理员个人凭据。
- 私域共享资料使用管理员目录字段展示“私域共享资料”及发布/检查状态。只有已发布、可读且可检索来源能进入检索选择；读取授权撤销后清空旧选择与证据，丢弃未完成的旧检索响应。未授权目录项明确显示“未获读取授权”。

## 验证

- `node --check src/wenjin/file_management/admin/app.js`：通过。
- `node --check src/wenjin/file_management/admin/identity.js`：通过。
- `node --test tests/admin_identity_auth_behavior.test.mjs tests/admin_frontend_catalog_refresh.test.mjs`：29/29 通过。新增慢登出响应竞态、私域候选不进入检索选择、撤权清理证据并丢弃迟到检索结果的行为覆盖。
- 本轮未操作浏览器或真实账号；视觉与真实服务端交互由根任务后续验收。

## 涉及文件

- `src/wenjin/file_management/admin/index.html`
- `src/wenjin/file_management/admin/app.js`
- `src/wenjin/file_management/admin/identity.js`
- `src/wenjin/file_management/admin/styles.css`
- `tests/admin_identity_auth_behavior.test.mjs`
- `tests/admin_frontend_catalog_refresh.test.mjs`
