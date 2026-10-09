# 实现记录：新版无状态 MCP 入口

## 2026-10-09：第二批协议入口

- 问题：现有 `register_tools`业务处理器包含旧协议标签、旧 `/files`路径，并读取 FastMCP Context 的 `session_id`；新版单纯启用 stateless HTTP 不满足 2026-07-28逐请求元数据与结果合同。
- 原因：新协议的版本/meta/header/结果语义由 MCP SDK v2实现，现有项目 handler仍服务旧SDK v1。
- 处理：新增`stateless_server.py`和`stateless_tools.py`薄装配层；复用旧工具处理、检索/重排、managed桥、release resolver、下载实现和 identity middleware；仅新版改协议标签、下载前缀和业务error的`isError`；Context代理把FastMCP内部UUID的`session_id`呈现为空。主`server.py`、`tools.py`和主依赖未修改。新版服务固定`/mcp-stateless`，旧session/resume头进入身份检查前忽略；协议头/body错配、版本不支持、未知方法、请求大小与错误JSON-RPC id分别处理。tool-call和个人取件响应设`Cache-Control:no-store`。
- 验证：独立TS SDK2.3.1真实loopback调用已在`sdk2-network-phase2.json`；协议定向测试及账号/版本合同在`sdk2-identity-release-phase3.json`与`phase2-3-targeted-tests.txt`。测试合成目录、合成检索向量和临时身份库；没有真实模型/语料。
- 问题修正：协议负例首次发现`_meta.protocolVersion=123`由SDK返回`-32020`而不是验收要求的`-32602`。原因是SDK在无效类型与传输头比较时先走了路由语义。入口现在在header/body版本比较前识别“存在但非string”的metadata字段，返回HTTP400/`-32602`并回显request id；缺字段继续交SDK校验。修复后定向协议及账号/版本测试通过，见[`phase2-3-targeted-tests.txt`](phase2-3-targeted-tests.txt)：6 passed、6 subtests passed。
- 审核修正：`create_stateless_app(api_key=...)`最初没有把静态Bearer模式传入`register_tools`，导致可信client id会被工具层当匿名。现在只在`api_key`存在且没有`identity_store`时设置`bearer_enabled=True`；账户模式继续由逐请求IdentityStore Principal决定身份。临时env捕获probe输出`static_bearer=True account_mode=False`。

## 2026-10-09：独立部署与容器构建

- 问题：FastMCP3.4.4要求`mcp<2`，与SDK2不能同环境。处理：新增独立`mcp-stateless/pyproject.toml`和141包`uv.lock`，精确pin FastMCP4.1.0/MCP SDK2.3.0；新CLI禁用隐式`.env`加载，不初始化身份DB，要求已有identity DB或Bearer key，TLS guard同时覆盖两种凭据。
- 部署处理：新增独立Dockerfile、Compose overlay、Caddyfile和PowerShell启动器；仅挂载只读检索/授权资产与身份volume；账号式managed桥不挂载旧file-admin secret，bridge使用请求个人bearer；新下载URL与`/mcp-stateless/files`同host路由；旧`/mcp`和`/files/*`保留到旧服务。
- 静态证据：Caddy2.10 `caddy validate`通过；Compose base+cloud+stateless静态合并成功，输出`mcp-stateless`、只读容器、供SQLite WAL sidecar使用的可写identity mount及新版Caddy路径；新版服务只挂载`model_api_token`，不附加file-admin secret。细节见`dependency-evidence/deployment-config-validation.md`。
- 容器证据：新镜像`wenjin-mcp-stateless:2026-10-09`成功build，ID与源码SHA见`dependency-evidence/build-log.md`；直接从镜像运行CLI `--help`通过。首次容器/TLS联验发现只读身份卷无法在WAL无sidecar时打开SQLite（HTTP500 / `unable to open database file`）；原因是SQLite reader也需在身份目录建立WAL/SHM协调文件。修复与后续独立容器复测见下节。
- 兼容性guard：原E盘项目venv只读执行新版CLI`--help`成功；显式调用新版runtime guard得到`FastMCP 4.x; found 3.4.4`清晰错误。新版SDK2专有request-body-limit导入改为lazy，避免仅看`--help`就因旧SDK API不存在而意外ImportError。原venv未安装或修改。
- 容器上下文：发现测试Client的嵌套`node_modules`不应进入镜像构建上下文；新增Dockerfile专用ignore，最终context为29.24KB，完整hash见build-log。

最终构建身份：`wenjin-mcp-stateless:2026-10-09` / `sha256:1f00cf6f96f22b68302845bad5eb8dbd2e44e976c720edf5029d9849d709fa62`，大小589,182,086字节。所有构建文件的SHA-256见[`build-log.md`](dependency-evidence/build-log.md)。

## 未解决项

- 新版生产业务模型、真实语料、真实授权库及撤权行为未在生产数据上验证；只验证隔离合成链路。
- 测试owner需用最终镜像复测TLS和真实WAL IdentityStore：身份卷当前是文件系统可写、SQLite `query_only` 逻辑只读。合成Python WAL probe已通过，正式容器热更新/撤权仍未验证。
- SDK可用客户端验收仅官方TypeScript Client2.3.1；Claude Desktop、Codex GUI、MCP Inspector未作为兼容能力声明。

## 2026-10-09：WAL身份库逻辑只读适配

- 容器联验问题：旧部署给新版容器的身份卷只读挂载时，临时真实`IdentityStore`保持`journal_mode=wal`且没有`-wal/-shm`文件。首次凭据解析返回HTTP500，容器异常为`sqlite3.OperationalError: unable to open database file`；管理端后续更新授权也无法被新版请求读取。
- 原因：SQLite即使只执行SELECT，也可能需要创建/维护WAL与SHM协调sidecar。只读文件系统阻止该底层动作。使用`immutable=1`会绕开一致性与热更新，不适用。
- 处理：新增`StatelessIdentityStore`，继承现有身份查询/授权读取，不改旧store；拒绝`initialize()`和数据库创建，每个连接均启用并核验`PRAGMA query_only=ON`。新版Compose仅把Identity volume改为可写，满足SQLite sidecar协调；身份SQL仍由SQLite强制只读。检索原始资产仍只读。
- 合成验证：[`wal-query-only-probe.json`](dependency-evidence/wal-query-only-probe.json)记录WAL、无预制sidecar首次读取成功、写语句遭拒、读取前后数据库正文hash相同，以及独立writer更新后下一reader读到新值。Ruff、py_compile、uv lock check通过；最终镜像CLI help通过。
- 边界：这表示身份数据库逻辑只读、目录文件系统可写；SQLite可产生/更新`-wal`和`-shm`。测试owner尚需用正式容器验证首次账户认证、授权/撤权热更新、写API拒绝和主库文件hash不变。

## 2026-10-09：工厂鉴权fail-closed

- 审核发现：公开`create_stateless_app`在未传身份库和静态Bearer时仍会创建无身份中间件的ASGI应用；这可能被直接导入后误用为可服务入口。
- 处理：在factory装配前要求`identity_store`或非空`api_key`，无凭据立即抛`ValueError`；生产CLI原有凭据检查保持不变。
- 验证：[`factory-auth-guard-probe.json`](dependency-evidence/factory-auth-guard-probe.json)记录调用`create_stateless_app()`的实际ValueError；Ruff和py_compile通过。此源变更触发最终镜像重建，镜像身份以build-log最新记录为准。
