# MCP 入口管理 UI 与 Docker 控制部署独立验收

日期：2026-10-14

## 结论

入口管理后端合同、身份/MCP HTTP回归、前端Node合同及broker overlay YAML静态检查通过。默认入口用SQLite revision CAS保存，只改变未来连接推荐；启停请求绑定用户看到的容器generation，并由独立broker对固定项目中的两个固定MCP服务执行。UI连接配置白名单投影不含MCP调用token；未知、冲突或过期操作不会冒充成功。

本轮仅做源码、隔离合同和静态部署审查。没有运行Docker Compose、连接Docker Engine、挂载Docker socket、启停真实容器、连接真实管理服务或改动真实身份/授权数据。

## 验证证据

后端入口合同：

```text
uv run pytest -q tests/test_mcp_entry_control_contract.py
```

结果：`29 passed, 1 warning`。覆盖admin session、reader拒绝、CSRF/Origin、无broker配置fail-closed、连接配置不含token、默认入口revision CAS、固定Compose项目/服务标签、generation冲突、容器状态准入、异步operation及outcome_unknown锁/复核。

关联身份/MCP HTTP回归：

```text
uv run pytest -q tests/test_identity_http.py tests/test_mcp_identity_http.py
```

结果：`3 passed, 6 warnings`。开发记录另提“受影响管理/API回归8项”，但本轮未取得那8项的准确测试名，因此只报告实际复跑的三项，没有声称覆盖8项。

前端定向合同：

```text
node --test tests/admin_frontend_catalog_refresh.test.mjs tests/admin_identity_auth_behavior.test.mjs tests/admin_credential_ui_behavior.test.mjs
```

结果：`48/48 passed`。入口合同覆盖默认切换CAS冲突、默认切换不发启停请求、配置allowlist过滤意外token字段、start/stop均提交观察到的generation、按终态轮询、unknown outcome/刷新失败持续禁用、operation 410过期触发状态复核且不报告成功、旧账号响应不回填，以及admin-session-only导航。

代码检查：`uv run ruff check src/wenjin/mcp_control src/wenjin/management_http/mcp_control.py`与三个入口管理模块的`py_compile`通过；前端与相关合同的`git diff --check`通过。

部署overlay经PyYAML读取并断言关键字段：overlay声明management和mcp-control-broker；Docker socket仅挂给broker且`create_host_path:false`；broker无published port、rootfs只读、移除全部Linux capabilities并启用`no-new-privileges`；Compose project由必需环境变量固定；bearer secret由Docker secret文件提供；management仅等待broker service_started。该检查不是Compose解析、容器运行或实际主机权限测试。

## 审查边界

- 管理路由使用`access="admin_session"`。未启用identity store时管理入口控制拒绝服务；缺少session为401，reader、停用/待首次改密身份无管理权限；变更请求继续经过Origin与CSRF校验。默认入口API只写推荐配置，不触发容器动作。
- broker硬编码`legacy -> mcp`、`stateless -> mcp-stateless`。每次操作按固定Compose project/service标签查找唯一容器并inspect；请求不能指定project/service/container ID。native Python进程不是带Compose标签的Docker容器，界面呈现unmanaged，不会伪报停止或尝试控制。没有model-node/Qdrant/native控制接口。
- 控制overlay只把Docker socket挂给broker且不发布宿主端口；broker与management通过internal `data-plane`通信。此网络含其他服务，但Docker控制范围仍受broker固定映射限制。Docker socket本身是高权限Engine通道，实际限制依赖broker实现和进程隔离；broker当前以`0:0`运行以访问socket，同时移除了Linux capabilities、使用只读rootfs并启用no-new-privileges。
- 当前`AGENTS.md`允许本轮个人迭代独立采用2026-07-28无状态入口，不得将其追溯到截止日前的项目叙事；旧入口仍按2025-11-25状态化协议显示。

没有浏览器手工视觉验收、Docker Compose配置解析/启动、Windows Docker Desktop socket访问或真实MCP协议连通性验证；容器running也不代表MCP健康。operation 410的UI行为由Node合同验证，broker TTL对真实Engine/运行中服务未作验证。
