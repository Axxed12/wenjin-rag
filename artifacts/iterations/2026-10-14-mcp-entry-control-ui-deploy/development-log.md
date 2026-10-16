# 2026-10-14 entry-control deployment preparation

## Scope and outcome

Reviewed the Windows host/Docker boundary for the management UI and the authorized stateless MCP iteration. Prepared two local images and isolated network/path probes. The broker-only control plane can proceed under `/root`'s live-operation ownership. The stateless container is not ready to share the current Windows identity DB because the synthetic concurrent WAL test failed closed with a SQLite disk I/O error.

## Evidence

- Disposable Docker container with `host.docker.internal:host-gateway`: native management `/api/health` = HTTP 200; model port `18766` = TCP open; Qdrant `/healthz` = HTTP 200.
- Read-only binds of `corpora`, `corpora-private`, `configs`, `data/indexes`, `data/releases`, and `raw_data` opened successfully in the stateless image. The probe read the active registry entry and SQLite metadata; it did not run retrieval.
- Built `wenjin-mcp-stateless:2026-10-14`, image `sha256:1b3e1df87fba57eb3cfd09b63296256ce4fe341651d4037a78d493972bc9ee34`.
- Built `wenjin-backend:2026-10-14`, image `sha256:6941765e2224efa160aa01e5a2518563d6c2bd9f3b93a2940ce33a90215d37cd` for the broker runtime; no broker container was started by this agent.
- Synthetic SQLite with a live Windows WAL writer: writable directory bind failed in the Linux `StatelessIdentityStore` reader at `PRAGMA user_version` with `disk I/O error`. Read-only directory bind saw a committed marker only while WAL/SHM sidecars existed; it failed to open when both sidecars were absent. The test used no production identity data.

## Current decision and next step

Keep native management `18777` and legacy MCP `18765` unchanged. `/root` owns the broker-only Compose profile, broker start, and native management environment reload. The native legacy service remains unmanaged by Docker. Keep stateless MCP unstarted and unadvertised as deployed until the identity-store boundary is fixed and validated; the current manager directory contains unrelated file-management databases and cannot be mounted broadly writable just to satisfy WAL.


## 本轮实现与运行接线（根 Agent，2026-10-14）

用户要求按 `docs/mcp-entry-control.md` 增加前端新版入口按钮并部署控制服务；后续明确同意两个版本同步身份认证。采用一个账号/个人凭据/授权权威，显式remote模式逐请求HTTP验证，不复制Windows身份库，不自动授权。

- 页面：两版入口卡、无token连接配置、推荐默认CAS与冲突提示、按generation启停/轮询/不确定结果复核；管理员导航与会话隔离。复制配置仅白名单字段。旧版原生进程保留且禁止Docker启停。
- broker：`wenjin-manual-mcp-mcp-control-broker-1` 已运行，镜像 `sha256:80114dbe55a354b57027ea8f21e12f0daf86617cb7a83ddb815b2fbeca8f4383`，只绑定loopback18769、socket仅broker；项目固定wenjin-manual-mcp。初始internal-only网络宿主端口不可达，改为专用bridge，仅重建broker；移除已空的旧专用网络，不动其他网络/服务。
- 管理：先接broker、再加载snapshot路由，共两次受guard重载18777。每次先核实root/身份DB/无后台活跃任务，做单独身份备份；第二次PID38648。旧版18765 PID19148不变，两次model identity对照均相同。证明进程/模型身份保留，非整库字节不变。
- 身份：GET `/api/mcp/identity/snapshot` 只接受个人MCP Bearer；原子凭据/权限读取后只投影当前可读活动项，精确文件拒绝优先于库继承；no-store，无其他用户/拒绝资源/credential hash。新版请求局部快照，不按用户缓存；401/503拒绝且无旧快照/static key fallback。
- 最终主环境前端Node48项根复跑通过；原入口29项加身份HTTP3项，共32项根复跑通过。bridge独立合同9项通过、1项因主环境旧SDK跳过，跳过未算通过；独立镜像FastMCP4.1.0/MCP2.3.0合成HTTP RPC `server/discover` 与 `tools/call:list_corpora`通过，根在镜像7483再次运行相同脚本成功。详见frontend-report、validation-report及identity-bridge-report；测试组有重叠，不累计成全量覆盖数字。
- 真实页面：用户登录18777后，桌面进入MCP入口、读取新版无凭据连接配置；390×844全页查看两版卡片，发现顶栏换行拥挤后调整为仅当前页标题、账号与连接状态。查看截图 `entry-config-desktop.png`、`entry-config-390.png`、`entry-mobile-final.png`。`evaluation-page-during-mobile-check.png`实际为评测页面误截图，不作为MCP验收证据；移动仿真点击两次落在邻近评测入口，故最终移动检查只报告视觉，不声称移动按钮动作通过。桌面真实启动/停止尚待新版正常启动后验证。未复制用户token、未签发/撤销真实凭据、未保存真实授权或更改默认推荐。
- 保留检查：snapshot-after 与 before 对比9条mcp_credentials、22条resource_grants行摘要均一致；登录会话/审计可以正常写入，不宣称身份整个DB不变。

## 本机新版启动与失败边界

选择性只读挂载corpora、corpora-private、configs、data/indexes、data/releases、raw_data，无真实身份DB或管理父目录mount。管理/模型/Qdrant经host-gateway访问，模型及Qdrantsecret单独Docker secret注入；secret值不输出、不写配置结果。

第一次镜像eb09启动因ReleaseCatalog初始化写只读数据库失败，停止新增容器后加入显式只读catalog路径，旧管理/旧MCP默认写路径保留。第二次镜像7483已越过catalog，Qdrant401，复用现有管理配置的Qdrantsecret后再次启动。第三次越过认证，但OGC 3D Tiles target的六个HTML正文段准入不一致，保持failclosed并停止新增容器；不重建/替换索引。只读catalog测试及原有两项扩展失败HEAD对照见release-catalog-readonly.md；同目标Windows准入通过、Linux原文hash通过但文本解析摘要不同，底层libxml2为Windows2.11.9/Linux2.14.6，见linux-html-admission-diagnostic.md。正在对独立镜像解析器做兼容对齐，未把容器running、health TCP或合成RPC当作真实资料客户端验收。


## 解析器兼容与最终部署（2026-10-14，根 Agent）

- 独立Linux镜像源码编译lxml6.1.3，libxml2 runtime/compiled均2.11.9。libxslt1.1.45不能搭配该libxml2（需要>=2.15.1），改用兼容1.1.39；不宣称与Windows的libxslt版本一致。加入-fPIC解决静态库链接失败。五个下载源固定SHA，helper仅改构建下载器取已验证本地包，不修改解析器C代码；缓存为忽略目录。
- 最终镜像 `wenjin-mcp-stateless:2026-10-14-libxml-2.11.9` / `sha256:74ca2f90a09b0aa492d0971d72ef6659792d372121f08dda0edeea6968ff7a03`。此前6c9镜像保留11.7MB uv源码缓存，加入BuildKit cache mount后重建，仅重建新版容器；独立验证最终镜像无cache/source/wheelhouse、编译工具、身份DB或代理环境，UID/GID10001；新镜像合成RPC再次通过，见[桥验证](identity-bridge-report.md)。
- 新版容器实际启动路径完成原有create_runtime/create_router索引准入，日志Application startup complete；没有放宽准入、改真实原文、重建索引或写Qdrant。运行状态running/healthy、restart_count0。该证据证明启动准入，不等于有效真实个人凭据的检索与语义验收。
- 继续检查时发现18777/18765均无监听；旧日志无退出原因，不能断言原因。确认两端口空闲、无活跃作业、broker/model可达后，使用[恢复脚本](restore_native_services.py)按原root/身份库/模型配置恢复二者；独立进程组与隐藏窗口，未停止模型/Qdrant。脚本最初管理ready时旧MCP仍在启动，both_listening=false为瞬时结果；后续[运行检查](runtime-check.json)确认管理200、snapshot匿名401、旧新版MCP匿名401。不再声称最终旧MCP PID未变化。
- 最终[保留检查](snapshot-after.json)确认9条mcp_credentials和22条resource_grants整行摘要与开工前一致。用户会话重载后需重新登录，测试窗口已打开并请求用户登录；不读取密码、cookie或个人token。此前桌面/390px视觉证据保留，但最新真实按钮动作仍待用户登录。

## 最终浏览器与控制闭环（2026-10-14）

用户在Edge测试窗口完成管理员登录后，桌面点击“MCP入口”，确认旧版推荐默认、新版端点18768、旧原生服务不可Docker控制；点击新版“查看连接配置”显示无token配置。真实broker按generation停止/启动均succeeded，见[控制操作](broker-live-actions.json)。随后再次仅停止本轮新版容器，刷新真实页面显示“已停止”，点击页面“启动服务”：实际管理认证/CSRF接口、broker异步操作、页面轮询闭环成功，页面出现“启动操作已完成”并更新为运行中，见[桌面截图](entry-ui-start-success.png)。最终新版保持运行，未改默认、未控制旧服务。

重新截图并查看[390px运行页](entry-running-390.png)，两卡单列、端点和按钮可见，无横向溢出；这是视觉检查，未重复移动按钮操作。停止按钮的浏览器原生确认框没有实测，停止功能由真实broker及既有前端行为合同分别覆盖；默认CAS仅合同验证，未修改真实推荐。未签发、复制、撤销真实个人token或更改授权。有效真实个人凭据Agent检索/跨版撤权在实际服务上的闭环仍未验收；临时authority/合成RPC及合同不替代这项。

本轮完成页面、控制服务部署与共享身份桥部署。真实HTML库启动准入通过，业务语义质量未重评测。最后检查管理200、新旧MCP及snapshot匿名401、新版healthy且restart_count0；凭据/授权摘要、model identity保持。code.md仍80行，本轮文档本地链接无缺失。
