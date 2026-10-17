# 按原始任务核验

2026-10-17。本表审核的是实现、隔离验证和交付准备；不把明确留给人工的客户端批准、目标机/公司网络验证写成已执行。协调者已检查最终Docker脱敏记录、实际镜像身份/当前207文件Hash与资源清理状态。原任务要求的实现和可自主完成验证已完成；依赖人工的项目均准备具体步骤并保留未验证状态。

| 原任务编号 | 当前实现或可审阅证据 | 范围 / 判定 |
|---|---|---|
| 一1：阅读当前文档 | 主目录AGENTS/PROJECT/code及近期记录；[接手日志](development-log.md) | 已执行；历史记录没有冒充本轮实验 |
| 一2：main、修改、文件占用 | main159a4a4；136输入文件Hash、独立Agent文件分工、[保留检查](main-preservation-check.json) | main未写入；基线另提交a19a2d5 |
| 一3：复用适用能力 | 既有模型准入、共享release、个人身份/授权、版本/来源校验继续复用 | 未重复模型/队列，未新加Redis、缓存或多副本 |
| 一4：隔离与不覆盖 | 独立worktree/临时账号和索引/Docker精确run_id | 现有服务与真实数据未改；精确Docker资源0，凭据删除审批拒绝有人工清单 |
| 二1：私域原生MCP | runtime、resolver、tools动态路由；两SDK网络合同和持续授权A→B | 通过；目标配置名脱离target_id也覆盖 |
| 二2：共享/managed生命周期 | release prepare-only/lost-pointer与替换中旧读/成功切换/失败保留 | 受影响合同通过，不迁移旧数据 |
| 二3：两版身份/ACL/版本/来源/错误 | [独立日志](test-development-log.md)：拒绝/撤权/旧版本/原文SHA，模型503→bridge→两MCP的isError | 通过；未授权身份泄漏等修前失败保留 |
| 二4：managed重排 | service+HTTP注入已有reranker/SearchPolicy，默认跟随search_reranker_enabled | 候选→重排→最终、禁用、错误传递及版本重叠合同通过 |
| 二5：临时输入 | 合成文档、IdentityStore、Qdrant Local/Server、独立服务 | 不读取/写入真实用户内容 |
| 三1：CLI/协议现状 | Claude2.1.296、OpenCode1.19.3实际调用；新旧SDK独立依赖 | 不假设CLI支持新协议，CLI采用旧入口 |
| 三2：最小配置和步骤 | [CLI说明](cli-agent-fixture.md)、[人工清单](manual-checklist.md) | 发现/工具/检索/原文/回答步骤齐备；命令区分已执行与待人工 |
| 三3：尽可能实际运行 | OpenCode连接；Claude discovered/pending approval；[GPT真实Agent](agent-answer-check.md)取证回答 | 没有额外生成API阻塞GPT测试；不计为完整CLI生成 |
| 三4：需要人工时不停止独立任务 | Docker/恢复/代码继续完成；临时项目批准、目标机/内网/人工答案清单 | 每项给机器、原因、步骤、预期和脱敏反馈 |
| 三5：分层报告 | [交付报告](final-report.md)分别列SDK、CLI、Agent、Docker、跨机 | 跨机和生产未验收 |
| 四1：分段计时 | 三轮harness阶段/锁/准入/推理/检索/请求记录；[HTTP任务队列时间线](http-queue-timeline.json) | 直接run_job的队列时间明确null，单worker另测约0.156秒 |
| 四2：单/多查询、单建库和混合 | 同条件before/after各15场景；fixture/model延迟/并发/策略/source hash齐备 | 合成确定性模型；不代表GPU容量 |
| 四3：替换/失败/冲突/取消 | file_management_service和managed delivery合同 | 通过；运行中取消不提前释放槽，build返回后清理，非逐批取消 |
| 四4–6：按测量最小改锁 | 只改embedding批次锁；[前后补丁](lock-scope-comparison.patch)及同条件JSON | 混合旧查询模型锁中位49.6→24.7ms；单建库无收益；保留rw锁/自定义adapter原合同/单推理 |
| 五1：Win10/11离线资产和启动诊断 | [操作说明](../../../docs/team-delivery-windows.md)、local-asset-inventory.json、七命令入口及镜像构建工具 | 有版本/hash与可信manifest核验，不把缓存盘点当摆渡包 |
| 五2：无公网隔离启动 | 新镜像--network=none构建/烟测，internal-only临时Docker服务 | 已有本机依赖镜像环境；干净目标机与GPU启动待人工 |
| 五3：异常退出 | 历史退出原因未知；新增run-record退出码/时间/PID与受限Docker诊断 | 不猜测历史原因，不据此更换架构 |
| 五4：完整业务恢复 | Local真实ReleaseService和HTTP合同；[Docker矩阵](docker-evidence/docker-acceptance.json)与[业务结果](docker-evidence/restored-business-report.json) | 42文件/2个受管版本/5个新空collection；原账号登录、授权/旧版/取证通过，不以marker替代 |
| 五5：研发候选题 | [六类题](candidate-questions.md)及JSON | API/版本/参数/扫描表/跨片段/无答案；来源答案待人工，不是黄金集 |
| 五6：禁止冻结集调参/公司指标归因 | 定向测试命令记录；固定策略/同条件比较 | 未跑冻结集，不将本机合成或0.6B归给公司8B |
| 六：记录与交付六项 | code≤120行、索引、三角色日志、final-report/manual-checklist及基线a19a2d5/业务1f97620 | 当前工作均保存在独立分支；文档同轮提交，不推送/合入/部署；main原改动保留 |

已知限制不隐藏：持续负载端到端公平性未证明（模型API FIFO，管理RLock不承诺FIFO）；运行中导入取消可能完成后续batch；无真实GPU容量、公司模型答案或生产验收；三个TEMP目录及Docker四份临时凭据删除被自动审批拒绝，保留人工清理项。源码提交有基准脚本EOF空行提醒，证据保留patch上下文/原始容器stdout空格；为保持实测source/hash和证据未美化重建。
