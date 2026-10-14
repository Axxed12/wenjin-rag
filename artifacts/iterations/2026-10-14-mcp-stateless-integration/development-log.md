# MCP Stateless 业务整合开发记录

日期：2026-10-14。集成 worktree `codex/integrate-mcp-stateless` 从本地保存点 `90a7e1f` 创建；本记录在挑取来源提交前初始化。

## 范围与边界

- 仅在本集成 worktree 整合 `f0e200e`、`ab53e7f` 中的stateless实现，并适配 `90a7e1f` 的model admission、managed client预算、个人凭据和identity DB行为。
- 不修改主目录或来源目录；不部署、重启正式服务或读取/写入真实业务数据；不覆盖当前 `models/providers.py`；不升级根项目依赖或 `uv.lock`。
- 保留 `/mcp` 的 `2025-11-25` 状态化协议入口。此批次仅按用户授权作为 `2026-07-28` 个人学习迭代集成，不追溯项目叙事截止日以前的能力。
- 源码由集成Agent负责；独立测试Agent负责测试代码与测试开发记录。源码冻结后不边测边改。本Agent只做静态检查；不运行功能测试。

## 初始化证据

- worktree起点：`90a7e1f`；分支：`codex/integrate-mcp-stateless`；开始时tracked工作树干净。
- 已阅读本worktree的 `AGENTS.md`、`PROJECT.md`、`code.md`、近期 `2026-10-14-mcp-credentials-ux` 与 `2026-10-14-git-checkpoint` 记录及开发日志索引。
- 来源计划：依次挑取 `f0e200e`、`ab53e7f`；若冲突按路径审查，文档以当前main文本为底增补，不整体接受ours/theirs；拒绝密钥、SQLite数据库和运行数据。

## 本轮整合

- 两个来源提交已按序 `cherry-pick -x`，分别记录为集成分支提交 `21cebfd`、`20f50b7`。冲突限于 `code.md`、`docs/development-log.md`；以当前快照为底保留近期状态后加本轮条目，没有整份采用ours/theirs。来源日期保持2026-10-09，避免把历史容器/SDK合同记成今天重测。
- `stateless_tools._adapt_tool_result` 增加对当前FastMCP `ToolResult` 的结构适配：读取 `structured_content`、`is_error`，将payload中的协议标签和下载路径翻译到新版，并保留结构化稳定错误码、中文`detail`和MCP `isError`。此前只处理dict会使`model_queue_full`、`model_queue_timeout`、`management_bridge_read_timeout`等typed error对象直接透传，丢失跨SDK合同。
- `StatelessIdentityStore._connect(*, allow_create=False)` 与当前 `IdentityStore._connect` 签名一致。它禁止创建/initialize，只打开已存在身份库；父实现只配置foreign keys、busy timeout并读schema版本，子类在交出连接前开启并核对`PRAGMA query_only=ON`。SQLite WAL/SHM sidecar仍需身份卷可写；应用级身份/授权/token写入由query_only拒绝。此为源码审查，未对当前真实身份库进行连接或写入。
- `deploy/compose.stateless.yaml` 显式接线模型read/connect/pool=`90/5/5`秒、FIFO等待数/等待上限=`8/30`、管理桥read/connect/pool=`210/5/5`秒，保留当前bridge read严格大于model read的启动校验。未改根项目`pyproject.toml`/`uv.lock`或`models/providers.py`；旧 `/mcp` 仍由当前main代码与2025-11-25生命周期提供。
- AGENTS/PROJECT/README/部署说明改为记录2026-10-14整合授权；历史实现/客户端/容器证据继续链接2026-10-09来源。新版个人迭代不归入2026-07-15前能力叙事。`code.md`将本批标作待独立验收。
- 初步静态检查：Python 3.12.14对三个`stateless_*.py`执行`py_compile`通过；`uv lock --check --project mcp-stateless`解析141个锁定包且通过；`git diff --check`通过；本轮新增/更新链接逐项检查16处全部存在；`code.md`共76行。
- 链接检查边界：对当前`PROJECT.md`、`code.md`和`docs/development-log.md`全量扫描还发现多条本地历史artifact链接目标在该集成worktree中不存在（例：2026-10-08记录）。这些引用原已存在于`90a7e1f`，不属于本轮新增链接；未在本批伪造或重写历史证据，需root决定是否另做文档裁剪/恢复。

## 阶段状态（历史；最终以交付报告为准）

- 已完成来源代码/独立依赖/部署配置整合与首轮源码兼容审查。上述静态检查已通过；功能/协议/HTTP/容器验收尚未执行。
- 当前未触碰真实服务、业务数据、身份数据库或部署；主代码 `models/providers.py`、根依赖和锁文件与`90a7e1f`相同。来源目录和主目录保持只读。
- 阶段状态：源码已冻结；测试Agent完成独立合同复验，结果见下文及测试记录。
- 独立测试Agent最终报告：新版合同、协议、身份/release、新旧时代隔离共16 passed、6 subtests passed、0 skipped；官方TypeScript SDK 2.3.1经真实loopback网络discover/list/call，覆盖个人凭据、ACL即时撤销、凭据撤销、release绑定、managed版本替换及下载原文SHA。新旧协议隔离包含在16项内，不重复计数。单独在main旧依赖环境对受影响准入/typed wire/管理员身份凭据合同8项通过。新合同首轮有fixture缺少合成`QDRANT_API_KEY`，修正后通过；新增wire合同曾漏`io`导入，修后整体组合重验通过。4 warnings来自合成Qdrant fixture调用上游弃用API；旧环境另有5 warnings。测试命令、环境版本、源码hash和具体边界见[独立测试记录](test-development-log.md)。
- Compose静态合同：从临时synthetic env运行base+cloud+stateless `docker compose config --format json`，断言stateless服务拿到model read/connect/pool=`90/5/5`秒、queue=`8/30`、bridge read/connect/pool=`210/5/5`秒，均匹配预期。只解析配置；未连接daemon、启动/构建容器或访问任何`.env`/真实secret。
- 本轮最终验证仍不含容器TLS/HTTPS、GPU、真实模型/语料/身份库、生产或UI客户端。2026-10-09容器与TLS证据仅是历史来源。独立测试使用临时合成SQLite、索引、凭据和loopback服务；依赖分别在隔离`mcp-stateless/.venv`及Node package安装，不改主uv环境/锁。
- 当前主分支 `src/wenjin/models/providers.py`、`src/wenjin/identity/store.py`、`src/wenjin/mcp/managed_client.py`、根 `pyproject.toml` 与 `uv.lock` 相对`90a7e1f`无差异。集成源码commit为`39d348e`；测试所用stateless源码hash见测试记录。新增独立身份卷允许SQLite WAL sidecar，而每连接`query_only=ON`，记录性合同通过。
- 文档基线债务：全量扫描当前worktree内已有的PROJECT/code/开发索引时，发现多条90a7e1f原有历史artifact链接目标不在该快照中。本轮新增/更新链接均已单独检查有效；root确认旧链接债务不阻塞本批，未批量复制旧日志/运行数据。
- 下一步：等待root对集成分支作最终审核并`--ff-only`合回main。此后如继续扩展容器联验，作为新的明确批次单独准备临时合成环境，不复用2026-10-09镜像结果。

## 实际交付

root完成独立审核并在主目录实际执行ff-only，main从90a7e1f到a5341d0，源分支/worktree保留；交付收尾只更新文档，已验收源码不变。完整范围、提交映射和保留状态见[交付报告](final-report.md)。没有推送、部署或重启。测试日志纠正pytest临时目录默认保留；误生成synthetic-only文件的删除被自动策略拒绝，均未提交运行数据或真实凭据。

- root审读要求将docs/mcp-stateless.md四段实测边界明确标为2026-10-09来源分支历史验收，并单列2026-10-14集成状态；已补充当前快照未做容器/HTTPS验收的说明。该文档澄清不改变业务源码冻结状态。

- 按root要求从来源提交 ab53e7f6d146df58c9c5cd9ae4b16741d76b7b73 按字节导出历史 artifacts/iterations/2026-10-09-mcp-stateless/handoff-snapshot.json（98,621 bytes，SHA-256 5A099E9315961BACE0538F8DA7FF27C7CF85E8B989E5E79EFD55C371DFEE41BD，Git blob 6d4c6e63ff39b394125867727e1ae8fc424ad05d）。未改JSON内容，也未复制旧源码；这是2026-10-09历史快照，非本轮验证。
