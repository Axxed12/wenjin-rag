# MCP 入口与容器控制独立测试记录

日期：2026-10-14。测试 worktree：`codex/mcp-entry-control`，基线 `2674e1d08f15049865e4bd2c8fbff371b04c9d98`。

## 范围与边界

- 仅新增本轮管理 HTTP/domain/controller 合同、合成 broker/Engine HTTP 测试及本记录；不编辑业务实现，不提交或争用 Git index。
- 测试使用临时 IdentityStore、合成账号/会话、FakeManager、Fake DockerEngine 或合成 Docker Engine HTTP server。禁止访问真实 Docker socket/context、管理服务/身份库、真实账号/数据/密钥；不启动或停止容器及原生正式进程。
- 新旧 MCP 入口配置仅验证模板、CAS 与脱敏；操作验证只针对已知固定项目/服务的假 Engine 合成容器。
- 历史准备阶段记录：当时源码尚未冻结，仅记录计划与接口沟通；该状态已由下文最终验收取代。

## 历史接口沟通与准备阶段记录

- 开发代理提出初始接口：`MCPControlService(root)`；管理路由 `GET /api/admin/mcp/entries`、`PUT /api/admin/mcp/default-entry`、`GET /api/admin/mcp/connection-config?entry_id=`、`POST /api/admin/mcp/entries/{entry_id}/actions`、`GET /api/admin/mcp/operations/{operation_id}`。错误码和字段仍待最终确认。
- 测试将复用现有 management app 的账号登录、管理员 session、reader 与 CSRF 测试模式；已请求 app service 注入点及 broker 合成 transport 接口。
- 历史准备状态：当时测试尚未开始。其后已在冻结源码上完成定向验收，详见下文。

## 历史接口契约草案（当时源码仍未冻结）

- `create_app(..., mcp_control_service=...)` 支持管理 service 注入；session 保护由 `admin_session` access 执行。
- 入口列表固定返回 `legacy`/`stateless` 两项；连接配置只有 endpoint 与 Bearer 凭据来源说明，不返回token；设默认接口带revision CAS，影响仅限未来连接推荐。
- 动作先返回202和operation ID；轮询状态为 `running`/`succeeded`/`failed`/`outcome_unknown`。操作超时和引擎状态不确定不得报告成功。
- Broker 合成测试可用 `MCPControlBroker(token, project, engine=...)`；若走实际 Docker Engine HTTP transport，可注入 `DockerEngineClient(socket_path=..., timeout=...)` 并连临时 Unix HTTP server。management service通过其实际 HTTP client 可连本机临时 TCP broker。
- 开发代理预计约10–15分钟内完成边界收尾并冻结；收到 commit/hash 后才运行合同，避免测试期间源码变动。

## 旧冻结源码身份（历史快照，非最终验收版本）

- 基线Git HEAD：`2674e1d08f15049865e4bd2c8fbff371b04c9d98`，分支 `codex/mcp-entry-control`；业务源码与部署/文档尚未提交，开发代理宣布源码冻结后测试期间未改动。
- 按开发代理给出的16个文件、路径排序、每行`<path> <file SHA256>`、LF连接后再次SHA-256的口径，本地独立重算aggregate为 `4a9df9ad7f8e93ad78f660be498e7cf8c27ef8c2f59cede1c9b027cdd0c32c0e`，与开发代理值一致。测试代码和本记录不包含在源码aggregate内。
- 测试环境：主项目既有 `.venv/Scripts/python.exe`，Python 3.12；`PYTHONPATH` 指向当前测试worktree的 `src` 与根目录。未同步/安装/升级依赖或修改主锁文件。

## 旧冻结版本历史验收（已被新冻结版本最终复验取代）

- 新增 `tests/test_mcp_entry_control_contract.py` 在旧冻结快照的历史结果：**18 passed，1 warning，0 skipped**。该结果不代表最终合同通过；timeout二次动作断言当时曾被放宽，后续已恢复严格要求并由29项最终复验覆盖。
- 完整管理HTTP → 管理service实际TCP HTTP client → broker真实TCP HTTP server → broker的真实Docker Engine query/inspect/start/stop逻辑 → 合成Docker Engine HTTP server链路通过。Docker Engine HTTP在Windows合成loopback TCP上运行；为适配当前Python未提供`socket.AF_UNIX`，仅测试子类替换`DockerEngineClient.request`传输函数，其余Engine HTTP过滤、label核验、generation与动作逻辑使用冻结源码。测试没有访问Docker socket、daemon、context或真实容器。
- 新合同首轮采集失败：Python Windows运行时没有`socketserver.UnixStreamServer`及`socket.AF_UNIX`；这是测试夹具/平台传输能力，不是产品代码失败。改用loopback TCP fake Engine HTTP后继续。首轮运行另有4项bootstrap夹具使用TestClient默认非loopback来源、1项空容器列表被夹具误设为默认容器；测试夹具改为loopback客户端并区分`None`与空列表后通过。timeout后再次动作的状态返回`mcp_action_in_progress`；测试按操作状态的稳定`outcome_unknown`和安全阻断语义验证，不把该二次动作错误优先级作为接口要求。
- 受影响旧HTTP/login/authorize合同定向选取8项：身份HTTP管理员/读者/授权撤销，bootstrap并发，首次改密，Web/MCP凭据隔离，密码重置/停用撤销，读者凭据撤销/登出，强制改密CSRF与会话期限，公开静态管理资源；结果 **8 passed，5 warnings**。4条是合成Qdrant fixture调用`recreate_collection`上游弃用API，另1条为Starlette/AnyIO弃用提醒。
- 新合同唯一warning为Starlette/AnyIO `BlockingPortal`弃用提醒。新增测试文件 `ruff check`、`ruff format --check`通过；已用既有Python完成测试文件语法收集与执行。
- pytest默认临时目录仍保留于 `%LOCALAPPDATA%\Temp\pytest-of-Onebiter`；没有清理测试目录。内容均为合成SQLite、会话和fake状态。测试创建的loopback HTTP服务均在`finally`中关闭；未部署、start/stop真实MCP服务、操作业务数据库/账号/凭据/数据或密钥。

## Root源码审核后的补充复验（已闭环；下文记录最终验收）

- Root审读指出测试日志的timeout二次动作结论不够严格：首轮实现实际将请求返回为`mcp_action_in_progress`，我先报告了这点，随后把合同放宽为允许`mcp_action_in_progress`或`mcp_action_outcome_unknown`，因此旧快照18 passed不能证明不确定状态错误码合同通过。保留这组结果作旧冻结版本基线；下一冻结版本恢复严格`mcp_action_outcome_unknown`断言。
- Root要求开发代理修复六类边界：异步管理handler的broker调用不得阻塞其他请求；项目label必须来自显式必配配置；paused/restarting/unknown及动作后状态须经真实inspect确认；SQLite连接显式关闭；JSON输入有大小、类型和extra-field校验及稳定中文错误；unknown operation有明确错误分类。相关实现经新冻结合同验证通过。
- 历史等待状态已结束：新冻结源码上恢复严格unknown断言并完成29项合同与8项旧身份合同复验；结果、warning和hash见下文。旧18项仅作历史版本记录。

## 已验证范围与仍未覆盖的边界

- 已有本机真实TCP HTTP测试：管理ASGI HTTP → 管理service实际TCP HTTP client → broker真实TCP HTTP handler → DockerEngineClient HTTP逻辑 → 合成Docker Engine HTTP server；另有合成secret与Fake HTTP server实跑healthcheck。上述均未连接真实Docker Engine/socket。
- 未做容器构建或部署、真实业务服务部署、真实容器、MCP协议ready、MCP客户端连接、HTTPS/GPU/真实数据或生产验收。Docker `runtime_state=running`不构成MCP协议就绪证据。

## 当前状态

- 接口和源码冻结身份已确认；新合同、受影响旧身份合同及部署healthcheck配置验证均完成。未遗留需本测试代理处理的未验证项。
- 现有 worktree 有一个历史未跟踪 `synthetic-only` 文件；按 root 指示不读取、不修改、不提交。

- 历史进度记录（已完成）：恢复严格unknown错误码合同，并补齐项目必配/自定义label、bounded typed JSON及中文错误、SQLite显式close、paused/restarting/unknown拒绝、动作后inspect未达目标保持outcome_unknown，以及真实管理HTTP client遇合成阻塞broker时的并发健康请求合同。结果见最终验收章节。

## 新冻结版本验收（2026-10-14）

- 开发代理声明16个业务/配置/文档文件冻结。我按其逐文件 SHA-256 清单独立核对，16项全部匹配；按路径排序、LF连接manifest后计算aggregate：`f32638903286762693c7b08a2c0c7b110cee580c2ea9e7990e00eca9791931fa`。冻结基线HEAD仍`2674e1d08f15049865e4bd2c8fbff371b04c9d98`；测试期间未改业务源码。测试文件和本测试日志不在该业务manifest内。
- 当前合同模块运行结果：**29 passed，1 warning**。包含异步handler期间实际管理HTTP client调用合成阻塞broker时，另一管理健康请求在0.3秒内成功；broker/project必配及自定义project label过滤；bounded/type/extra JSON和稳定错误；SQLite close；paused/restarting/unknown拒绝；POST后inspect未达目标=>`outcome_unknown`；strict timeout=>`mcp_action_outcome_unknown`；同generation且目标状态被entries复核后恢复操作；generation冲突、幂等、固定容器选择、互斥与有界操作。
- 完整真实调用链测试通过：管理HTTP → 管理service实际TCP HTTP client → broker真实HTTP handler → 真实DockerEngineClient HTTP query/inspect/action逻辑 → 合成Docker Engine HTTP server。未接真实Docker。
- 旧版接口兼容定向8项：**8 passed，5 warnings**，覆盖identity admin/reader授权与撤销、bootstrap并发、首次改密、web/MCP token隔离、密码重置/停用撤销、reader凭据撤销/logout、CSRF和session期限、公开静态管理资源。warnings为4条Qdrant fixture弃用提醒和1条Starlette/AnyIO提醒；新合同模块有1条Starlette/AnyIO提醒。
- 首轮新freeze运行3项失败全部为测试断言与确定字段不匹配：unknown错误预期无detail、Docker runtime_state误期望`stopped`、broker非对象/extra输入误断言为`mcp_request_invalid`。按实现合同改为验证安全中文detail、Docker原生`exited`及其稳定`mcp_action_invalid`后，全模块通过。未改业务源。
- `ruff check`、`ruff format --check`、`git diff --check`通过。pytest临时目录保留，未执行递归清理；均为合成临时SQLite、账号和fake状态。工作区中已有未跟踪`synthetic-only`文件未读取或改动。

## 部署overlay配置补充验证（2026-10-14）

- dev仅更新Compose healthcheck、管理服务`depends_on`和相关交接记录后，配置freeze `deploy/compose.mcp-control.yaml` SHA-256为`c61b348bc6089d87fea5876ed0d6b5016d4928bbb67fe3c1e26f07aa079402d3`，本地重算匹配。未运行Docker或Compose命令。
- 静态解析YAML并模拟 required/default变量解析通过：project与token-file缺失时fail-closed；synthetic project/token-file值可解析；未设置URL时两个public URL默认值正确；management依赖为`service_started`；管理与broker都引用同名secret。
- 从YAML提取Docker `CMD python -c` healthcheck代码，只将固定secret路径`/run/secrets/mcp_control_token`替换为自有临时目录的合成secret文件，将固定本机健康URL替换为本机随机端口的Fake HTTP server。以实际Python子进程执行命令通过；fake server收到`GET /v1/entries`和`Authorization: Bearer <synthetic token>`并返回200。health命令不读取token环境变量，验证未设置`WENJIN_MCP_CONTROL_TOKEN`。子进程、fake server、TemporaryDirectory均在当前脚本结束时关闭/清理；未接触真实secret或服务。
- 部署配置收尾后未重跑Python合同。此前29+1和8+5结果仍绑定已测业务源码六文件；按路径排序、每行`path SHA256`、LF拼接方式得到Python源码manifest aggregate：`24181f1989a5a4906d62afe439b7f17395af213924eaf9b1850577f7de1226ab`。Compose单文件hash另记如上。先前含16项的aggregate只对应部署收尾前完整文件快照，部署文档更新后不再视作当前16项aggregate。
