# 内网团队交付补齐

日期：2026-10-17。本轮实现、隔离验证与交付准备按[最终报告](final-report.md)所列范围完成，不代表生产验收；人工批准/目标机与被自动审批拒绝的清理见[清单](manual-checklist.md)。下文保留各阶段过程，当前结果以末尾协调复核和最终报告为准。

## 接手与隔离

- 已读主目录AGENTS.md、PROJECT.md、code.md及10月12日并发/准入、10月14日入口部署记录；main HEAD为159a4a4，实际工作文件另有未提交后续实现。未沿用10月14日初轮“新版未部署”的结论：现有Docker只读列表显示新版及broker容器存在。
- 当前其他可见项目对话空闲，当前根任务无既存运行子Agent。本轮新派GPT-6 Luna high：delivery_core负责业务，delivery_recovery负责离线/恢复，delivery_independent负责独立测试；协调者审查和汇总。
- 独立worktree：`C:/Users/Onebiter/.codex/worktrees/team-delivery-20261017/rag-project`。先从main建立，再复制实际主目录当前源码、配置和必要历史Markdown；未复制真实数据/授权/原文/索引、.env或secret。补扫被忽略的源码，不能只凭git status；路径/Hash见input-source-manifest.json。部分历史脚本差异仅来自CRLF，交付时不混入无关改动。
- 主目录未写入；既有服务不重启/停止。既有GPU占用意味着不再启动第二套GPU模型；本轮性能结果必须明确模型、设备和合成/真实边界。

## 当前查实的缺口

1. 管理私域目录的有效路由没有完整进入原生MCP runtime/list；共用有效选择与授权版本语义，不能复制资料或扩大权限绕过。
2. managed搜索没有注入现有shared reranker/policy；补可配置接线并复用provider/模型准入。
3. 完整备份恢复仍仅有历史marker证据；需临时完整业务数据及恢复后登录/授权/版本检索。
4. 离线资产统一Hash入口、启动/退出诊断、实际客户端隔离配置和研发候选问题待补齐。
5. 上传/查询锁等待尚未分段测量。先同条件观测，再决定必要改动，不把模拟耗时作为GPU性能。

## 验收分工与完成门槛

- 核心：两版私域发现/查询/取件、ACL越权及撤权、版本拒绝、rerank候选/最终结果和错误，不伪装空证据。新测试由独立Agent维护。
- 并发：固定输入单查询/多查询/单建库/混合；替换中读取、成功/失败切换、候选冲突、取消后执行及资源释放；记录实际无法观测的阶段，不填推测数字。
- 客户端：本机Claude Code 2.1.296，OpenCode不在PATH。官方说明经agent-reach网页读取确认HTTP/Bearer配置，但不能据此断言2026-07-28支持；优先旧版入口。不得读现有私人凭据/全局配置。协议SDK、CLI、真实LLM、跨机分别登记。
- 交付：离线资产/版本Hash、Windows Docker步骤和诊断、完整临时业务恢复、候选题/人工复核表。缺公司机器/模型条件不阻塞其余工作，但待人工清单必须可执行。
- 最终审核逐项核实本用户目标，不以局部green代替全目标；code.md和docs/development-log.md由协调者统一更新。

## 证据入口

- [输入源码清单](input-source-manifest.json)
- 原始历史报告在主目录`artifacts/iterations/2026-10-12-concurrency-baseline/final-report.md`与`2026-10-14-mcp-entry-control-ui-deploy/development-log.md`，属于历史，非本轮重测。

## 协调复核与当前结果

- [业务开发与冻结记录](core-development-log.md)、[独立测试记录](test-development-log.md)、[交付恢复记录](recovery-development-log.md)分别保留分工证据；未通过的首轮私域合同见`failures/private-route-initial-failures.md`。
- 用户确认生成端可以由子Agent承担；已实际执行[GPT Agent经MCP取证回答](agent-answer-check.md)，由新建且未给答案的GPT-6 Luna high调用SDK网络MCP、读取原文并作答。该项不再等待额外生成API。合成检索与真实生成的范围分开，不替代Claude/OpenCode完整作答或公司模型验收。
- OpenCode 1.19.3实际`mcp list --pure`连接通过；Claude Code 2.1.296加载独立项目配置后显示Pending approval。首次无配置输出和参数错误保留在测试日志，不误记为协议不支持。临时fixture进程、监听和凭据描述文件已清理。
- 同条件三轮合成锁比较只使用`contention-before-final-3rounds/managed-contention.json`与`contention-after-final-3rounds/managed-contention.json`；协调者核对配置/harness一致，源码差异仅为[模型锁范围补丁](lock-scope-comparison.patch)中的五处。探索性目录不纳入结论。混合首个旧版查询模型锁等待中位约49.6→24.7ms；合成延迟不代表GPU服务容量。
- 取消合同已实际观察首批embedding执行中HTTP返回202/running/cancel_requested，后续批次仍可能执行；完整build返回后才清理候选，保留旧active。故不宣称逐批即时取消，已启动推理槽位不会提前释放。
- 本轮代码与证据仍在独立worktree，主目录未写入。最终本地提交与工作区状态待验收收口后登记；没有推送或部署。
- 自动审批拒绝原生PowerShell删除三个已核对为本轮所有、位于TEMP下的目录：`wenjin-team-delivery-fixture-log-e8c76b1e68544cfa9f357a270d61dabd`、`wenjin-team-delivery-mcp-bs9aj0jm`、`wenjin-team-delivery-tools-20261017`。返回仅为`blocked by policy`，没有具体理由；未用其它工具绕过。此前对应服务/fixture凭据描述已清理、无相关进程；这三份残留需人工检查后删除，临时npm安装不再被说明为已删除。
- 本地基线提交`a19a2d5`保存接手时已有42文件；本轮32份源码/测试提交为`1f97620`。源码stage检查提示基准脚本636行EOF多一空行；为保持已实测harness与镜像source hash不变，保留该无功能影响格式提醒。证据stage中patch上下文和原始容器stdout空格也原样保留，不宣称全量diff-check无提醒。
- 最终独立验收收口：主环境27通过/1旧fixture失败后单独修正通过，受影响组16通过，HTTP worker时间线1通过；现代环境4通过。有重叠，未相加。精确命令与未单独保留完整stdout的证据限制均写入测试日志。
- 最终Docker恢复证据见[docker-acceptance.json](docker-evidence/docker-acceptance.json)：42文件、2个受管版本、5个新空Qdrant恢复collection、原三个合成账号登录/密码记录一致、200/409/403及实际旧MCP取证。漏Origin的403、原seed读者密码后缀与无OSG授权的身份选择属于夹具修正，没有改恢复库密码或扩权。
- 协调者独立重算207文件源码Hash，核对三张新镜像ID/source label一致，精确Compose项目容器/网络/卷均不存在；见[source-freeze-audit.json](source-freeze-audit.json)。model-node仅import，无GPU。三交付镜像保留，不是运行部署。
- 隔离Docker四份临时凭据文件删除同样被自动审批拒绝（仅返回blocked by policy）；目录和准确文件名列入人工清单，未绕过、未将值写入证据。对应Docker服务和存储已销毁。源构建上下文保留供复核；不会错误声称凭据全部删除。
- 目录变化：操作工具沿用`scripts/operations/`，`scripts/team_delivery.py`为兼容入口；新增`scripts/experiments/measure_managed_contention.py`、`tests/fixtures/team_delivery/`与两个acceptance夹具/CLI脚本，详细结果仅在当轮artifact。根README增加Windows交付入口并让旧“未部署”语句指向当前状态，不调整业务目录或扩展前端。
