# 内网团队交付补齐报告

2026-10-17；本轮实现、隔离验证与交付准备按下述范围完成。后续已按用户授权快进合入main，见[合入记录](../2026-10-17-main-integration/development-log.md)，未推送/部署。此报告描述个人迭代，不追溯为2026-07-15前公司能力；目标机/跨机/完整CLI生成和自动审批拒绝的清理仍需人工。

## 实际变更

1. 原生新旧MCP共用管理端有效私域路由。运行中选定版本从A改B时重新形成一致快照；不再把旧A标成可用或回退到旧A证据。配置文件不要求与target同名。共享release和缺活动指针拒绝的规则保留，不迁移/复制真实资料。
2. 私域原文下载绑定target原文快照根，修复错读`raw_data`导致的409；未授权响应不泄漏私域corpus身份。保留逐请求账号、授权、版本、原文hash与资料来源检查。
3. 受管文件检索接现有共享reranker与SearchPolicy；在线默认跟随`search_reranker_enabled`，关闭时不重排。复用模型provider/准入队列，模型繁忙错误经管理HTTP及新旧MCP返回可识别错误，不伪装正常空证据。
4. 标准Qdrant/BM25建库只在每个embedding批次持有模型锁，索引写入不再占整个模型临界区。检索/激活仍保留读写锁，未声明批次锁能力的自定义adapter保持原合同。没有增加GPU推理并发。
5. 新增`POST /api/jobs/{job_id}/cancel`及持久取消意图。排队导入清理后取消；运行中导入等待同步build返回安全点再清理候选，保留旧版。已经执行的推理不会被假装释放。当前可能继续后续embedding批次，不承诺即时或逐批取消；删除任务不由此取消接口处理。
6. 新增资产版本/hash清单、停写备份/新目录恢复、Qdrant Server快照操作工具、源码绑定镜像准备、限定容器诊断与进程退出记录入口；MCP CLI支持显式项目根。备份不包含.env/secret，身份库备份需受控存储；可变SQLite用backup API，封存BM25按字节复制保留seal hash。

## 验证与证据

| 层次 | 当前结果及边界 |
|---|---|
| 新旧MCP协议/SDK | 合成私域发现/检索/原文、普通用户拒绝、撤权、持续授权下A→B和managed模型错误实际网络合同通过。主环境首个组合27通过/1旧fixture失败，修fixture后该项通过；后续受影响组16通过、HTTP调度1通过，新环境4通过；组间重叠不累加。命令和证据见[独立测试记录](test-development-log.md)，完整stdout未另落盘，日志为会话工具结果转录 |
| 真实Agent生成 | 新建GPT-6 Luna high，未提供答案，实际经MCP发现/检索和HTTP原文取证，回答正式档案36个月、临时副本90天并引用原文；见[完整轨迹摘要](agent-answer-check.md)。这是合成检索+真实生成，不是公司模型质量验收 |
| OpenCode 1.19.3 | 真实CLI `mcp list --pure`连接通过；该CLI完整生成未执行 |
| Claude Code 2.1.296 | 实际独立项目配置显示Pending approval；未绕过批准，不能计为连接/工具作答通过 |
| 完整临时恢复 | Local恢复覆盖真实身份/授权、两文件版本、双索引、ReleaseService封存发布/原文。另实际Docker恢复42文件/2个managed版本/5个Qdrant collection，三个原账号登录、密码hash一致、授权检索200/旧版409/无授权403；旧MCP实际list_tools、reader文件v2和admin共享release A取证通过。见[Docker矩阵](docker-evidence/docker-acceptance.json)、[业务I/O](docker-evidence/restored-business-report.json)和[恢复日志](recovery-development-log.md)。容器内部网络，不代表宿主端口或跨机 |
| 目标机/真实业务 | 干净Windows 10/11离线安装/GPU启动、跨内网机器、公司8B和真实文档语义验收未执行；[人工验收清单](manual-checklist.md)给出机器、步骤、预期与反馈要求 |

首轮失败没有删除：未授权身份泄漏、原文错根、A→B旧路由可用性、封存BM25被SQLite backup改字节及CLI配置差异均保留原因/修复记录。无冻结评测集、无无关历史全量重跑、无真实数据/授权写入、无现有服务重启。

## 同条件性能对照

只采用相同harness、同合成资料SHA、30ms确定性模型操作、3轮、候选10/重排池30/最终3、单推理与队列8/30秒的[修改前](contention-before-final-3rounds/managed-contention.json)和[修改后](contention-after-final-3rounds/managed-contention.json)。源码对照见[锁范围补丁](lock-scope-comparison.patch)。

| 三轮中位数 | 修改前 | 修改后 |
|---|---:|---:|
| 混合建库中首个旧版查询模型锁等待 | 49.6ms | 24.7ms |
| 该查询完整耗时 | 约124ms | 约107ms |
| 混合场景总耗时 | 约328ms | 约317ms |
| 单建库耗时 | 约238ms | 约240ms |

该结果支持缩短建库占锁能减少合成查询等待；没有证明真实GPU吞吐或十多人容量。阶段计时重叠，不能累加。该脚本直接调用`run_job`，其job_queue_wait为null；独立HTTP调度计时另记，不用零代替未观测。模型API既有准入队列按事件循环入队顺序FIFO；管理层Python RLock不是FIFO承诺，因此整条查询/建库链路的公平性和持续负载下饥饿上界未证明。

## 使用入口与剩余边界

- [Windows交付、备份恢复和诊断](../../../docs/team-delivery-windows.md)，可调用入口`scripts/team_delivery.py --help`；Qdrant Server另见`scripts/operations/qdrant_server_snapshot.py --help`。
- [临时CLI与Agent夹具](cli-agent-fixture.md)。合成服务已清理，重新运行时会生成新随机端口与凭据，旧65145不能复用。
- [候选研发题](candidate-questions.md)：精确API、版本差异、参数边界、扫描表格、跨片段、无答案；来源/答案需人工审核，不是黄金集。
- 历史原生服务退出原因仍未知；新启动日志工具有助于下次定位，不把新工具当作历史根因证明。
- 本轮无推送或部署。三镜像绑定源码`639794c5b660851559d2fc0894d80356f9528f381e90cfe027e58c33463a2ab6`，backend/model-node/stateless分别为`4b512b90…`/`a30f3508…`/`3e684d3b…`；完整ID和207文件Hash见[协调者只读复核](source-freeze-audit.json)。model-node只import，无GPU/推理。镜像重建和隔离容器验收不会自动更新现有运行服务。

## 提交与工作区

- main起点159a4a42a7169bab87ab149b8e39aa6464ed987e；独立分支`codex/team-delivery-20261017`。接手实际源码已单独保存为本地基线提交`a19a2d5`（42文件），使用独立worktree索引读取main文件，不写main的文件、索引或分支。
- 基线包含main实际未提交后续源码，按[输入清单](input-source-manifest.json)保留，不将全部差异记为本轮原创。
- 主目录136份输入源码快照只读复核未发现变化，见[主目录保留检查](main-preservation-check.json)。主目录原有未提交文件及运行数据保留。
- 本轮业务与测试已提交`1f97620`（32文件），交付文档提交`d5b321d`。用户随后授权保存并合入main，main先快进到已有基线`a19a2d5`再快进到`d5b321d`；原主目录开发完整保留，无业务源码冲突。未跟踪运行数据保留，未重启或部署服务。
- 协调者重新计算207文件源码总Hash，与三镜像label/ID一致；精确Compose项目的容器、网络、卷均为0，合成fixture镜像已删除，三个交付镜像保留可导出。
- 临时凭据四文件及三个旧临时目录删除被自动审批拒绝，返回仅`blocked by policy`；未改工具绕过。准确路径与人工删除范围见[清单](manual-checklist.md)，内容未提交。工作树保留未跟踪的探索性计时JSON和三份源覆盖快照，不纳入正式比较或本轮提交；修前正式JSON、补丁及失败证据已单独保留。
- 源码提交静态提醒为基准脚本EOF多一空行；为保持已实测harness/镜像source hash未美化重建。证据提交还原样保留patch上下文空格与容器stdout尾空格，完整diff-check会提示这些格式，不当成功能失败或修改原始证据。文档链接核查无缺失；历史资源在main中的存在与本轮新证据分开记录。
