# MCP入口管理后端开发记录

日期：2026-10-14。工作区为`codex/mcp-entry-control`独立worktree；主工作区保持只读。本轮只提供可供管理前端调用的backend API，不修改前端，不部署新版MCP。

## 问题与处理

- 管理页面需要选择旧`2025-11-25`或新版`2026-07-28`入口作为未来连接推荐，并分别启动/停止指定MCP容器。两种协议配置共存；切换默认项不影响已有Agent，不隐式启停或迁移连接。
- 新增`/api/admin/mcp`接口和本机SQLite revision CAS，保存于被忽略的`data/file-management/mcp-control/settings.sqlite`。路由额外要求账户身份库中的管理员session；变更请求继续执行既有同源Origin与CSRF检查。无账户模式fail closed。
- 新增单独的Docker broker服务，严格Bearer认证、必需且严格校验的Compose项目环境变量；入口硬映射`legacy->mcp`和`stateless->mcp-stateless`。broker独占Docker Engine socket，管理容器只通过内部HTTP调用；动作仅为start/stop已有的唯一受管容器。
- 每次动作重查Compose project/service标签，generation仅作为可选的过期状态比较值。每入口单动作锁、30秒Engine预算、20秒stop grace、60秒管理动作读取预算；超时进入`outcome_unknown`且阻止同入口新动作，只有同generation达到明确目标状态后才释放。paused/restarting/removing/unknown等状态不冒充停止；Engine接受动作后额外inspect核实。operation容量100、终态保留15分钟；broker重启造成的旧operation返回410。
- 管理API JSON对象限制8 KiB并拒绝额外字段；全部SQLite、urllib及profile同步工作在线程池执行。SQLite连接显式关闭。wire errors带安全中文detail，运行状态另有中文label。
- 入口列表明确Docker `running`不等同MCP协议ready。不存在或多匹配容器显示稳定错误；旧版Python原生进程呈`unmanaged`，不由Docker broker伪控。

## 变更目录

- `src/wenjin/mcp_control/`：profile存储、管理端broker client及独立Docker broker。
- `src/wenjin/management_http/mcp_control.py`：管理API handlers；`app.py`注入并登记路由，`http_common.py`增加仅该组路由使用的身份会话强制模式。
- `deploy/compose.mcp-control.yaml`、`deploy/.env.mcp-control.example`、`deploy/with-secrets.sh`：可选broker部署overlay和secret读取。
- `docs/mcp-entry-control.md`：接口字段、认证、操作与运维边界。

## 验证与边界

- 使用既有主环境Python完成新旧管理模块`py_compile`和定向Ruff：通过；`git diff --check`通过。经root代码审查发现并修复operation错误优先级、async eventloop阻塞、项目名适配、容器状态复核、SQLite句柄及请求体校验问题；另禁用broker client的系统代理/redirect并严格检查Bearer。独立合成入口控制合同29项通过，受影响管理/API回归8项通过；测试覆盖账号/CSRF、CRUD输入边界、revision CAS、固定项目标签、状态复核与unknown锁、异步worker及合成Docker HTTP链路。3项首轮失败均为测试预期修正（中文detail、Docker原生`exited`状态、畸形JSON错误分类），未修改业务源码；最终测试由测试Agent记录。
- 部署overlay静态收尾：healthcheck读取Docker secret文件而非依赖entrypoint导出的进程环境，管理服务只等待broker启动以保留基础管理可用性。合成secret文件与本机Fake HTTP 200 health probe通过；未使用Docker/Compose运行验证。
- 未挂载Docker socket、未连接Docker Engine、未启动或停止真实服务、未读取真实身份数据库或`.env`、未部署/推送。
- 当前版本处于合入前审核状态；本地提交与fast-forward由协调者完成。
- 测试完成后仅修剪`src/wenjin/mcp_control/__init__.py`和`management.py`的额外EOF空行；测试所对应版本与当前源码除这两处纯空白差异外一致，因此未重跑合同。修剪后两文件定向Ruff及工作树`git diff --check`通过；协调者需更新暂存内容后再复核暂存区空白检查。
