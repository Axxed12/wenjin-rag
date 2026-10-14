# 模型有界准入：协调与审核

日期：2026-10-12。业务开发与独立验收分别由 GPT-6 Luna high 子 Agent 执行，主 Agent 只协调和审核。

## 开发依据与范围

- 已阅读 AGENTS.md、PROJECT.md、code.md，以及 2026-10-12-concurrency-baseline/final-report.md。原 collision.json 为共享成功、受管零证据；关联服务日志明确记录模型 embedding 与管理 /api/mcp/search 的下游503。原文件保持不变，本轮不重新执行128条矩阵或冻结集。
- 最小方案为同一模型服务所有 query/document embedding 与 rerank 共用一个 FIFO worker，GPU 单执行；最多8个等待请求、等待30秒。客户端读取90秒、管理桥读取210秒是待验证初值，不是最优或总查询SLA。
- 按模型服务准入、管理/MCP错误与超时接线、独立验收三批交付。排队拒绝需明确错误，不以正常空证据代替；不加无限重试、Redis、语义缓存或GPU并发。

## 第一批设计审核

- 队列公平性限定为实际进入模型服务队列的单次模型调用FIFO；不保证整条查询或建库任务的优先级、公平时长。
- shutdown停止接单，未执行请求拒绝；实际已执行推理drain后才关闭模型。调用方取消不能解除worker的执行所有权。
- HTTP断开一般不会自动取消ASGI handler，不能仅依赖Starlette任务取消；排队请求需要显式监听http.disconnect。独立测试须覆盖真实TCP断开，而不仅future.cancel。
- 当前修前源码新增admission.py尚未接线；冻结复现应注明此差异，不能称hash等同原基线。测试owner冻结旧行为源码后开发可并行继续。

## 待验收

单请求、固定混合查询、队列满/超时与错误传递、排队取消/断开无泄漏、已开始推理断开仍单执行、查询与建库竞争、授权/版本/证据身份。GPU与Docker由独立测试Agent唯一持有，使用隔离数据与只读模型权重。MCP协议取消双响应异常仍列后续，不扩大本轮重构。

## 第一、二批代码审核与冻结

- 已审读实际worker/deadline、HTTP断开监听、runtime RLock、typed provider错误、管理HTTP503及MCP ToolResult错误接线。运行中推理由独立worker持有，调用方取消只影响等待者；事件控制合同与真实TCP断开合同由独立测试Agent报告11项通过，真实GPU链路尚待验证。
- 发现并交由开发修正：空闲请求被错误计入waiting容量、shutdown未唤醒等待者、过期项分派后未清active、取消未执行reservation仍可能启动、唤醒事件被提前清空。测试保留对应修前失败，不以最终通过覆盖。
- 修前窄复现run为`20261012T080914Z-a44260`，其退出检查发现开发期间源码改变；仅作症状补充，不能作不可变源码验收，原基线仍是主要依据。修后完整src与runner冻结后只读挂载，不再挂可变工作区。
- 初报冻结SHA因路径/NUL算法不一致作废；使用原基线source_tree_hash算法，双方确认当前src为`c9c8cada8b37e0ccc5f97008f077872707fe377f70d14c266212827d4b758745`。这是源码冻结指纹，镜像和实际运行身份仍由独立验收记录确认。
- 原有ignore规则会漏列providers.py和Markdown；本轮实际改动以开发记录及冻结源码清单为准，未扩大ignore整理或创建Git提交。
- 后续配置审核发现bridge可被手动调到小于queue/model read预算；开发补上remote模式bridge read必须严格大于配置model read的守卫，并拒绝非法模型read。保留local模式原配置空间，不增加identity请求或重试。GPU尚未启动，修正后重新冻结完整src，新候选SHA为`c0aa7dfcdb151c0f7f957a66b889bb3ba1406c4ffa1f3d5e671d6b7154f5a674`；此前c9c8候选不作为最终运行源码。

## 最终证据审核

- 独立测试最终24项admission合同通过，覆盖同队列query/document/rerank、精确容量/FIFO、取消/超时/断开/shutdown、HTTP503到管理桥及MCP错误、超时配置守卫。另一个选定provider/managed bridge回归批24项通过；两批按各自命令记录，不将历史7/11项重复累计。详见[test-development-log.md](test-development-log.md)。
- 缓存后端镜像中的Streamable HTTP探针确认initialize/initialized状态化会话与2025-11-25协议，错误回包包含`result.isError=true`、`structuredContent.error=model_queue_full`和`evidence_status=not_retrieved`。探针初次失败是测试SSE/session处理问题，未扩大协议重构。
- 主Agent审读[最终小批结果](runs/20261012T084409Z-6be28c/targeted-results.json)：共享和受管单请求各1条正确证据；同时共享/受管各1条正确证据，均无busy rejection；旧版本请求invalid_target且无证据，未授权身份/default deny通过。并行查询和新文件入库均成功，入库后MCP读回marker及file/version身份正确。
- [运行清单](runs/20261012T084409Z-6be28c/run-manifest.json)中管理/MCP/模型三服务实际导入根均为`/app/src`，完整SHA与最终冻结c0aa一致。只读模型权重、一份GPU模型服务、独立Qdrant/data/identity卷；真实6333及其他服务持续运行。
- [清理](runs/20261012T084409Z-6be28c/cleanup.json)显示项目容器/卷/网络均0，临时根目录与凭据移除，冻结hash运行后仍一致。主Agent再次核对docker ps，其他已有服务保留。原始修前失败证据未修改。
- 真实查询/入库仅记录客户端区间重叠0.609秒，未观测服务端排队时间或实际GPU推理竞争；不能以此宣称性能容量/等待SLO。资源互斥和顺序执行由确定性端点与线程生命周期合同支持。公平仅按单进程单次模型调用FIFO，整条query/build优先级与跨副本仲裁未实现。MCP取消双响应异常仍为后续任务。

本轮业务实现及上述限定范围验收通过。8/30/90/210初值仍非最优选型或生产验收；未重跑整套基线、冻结评测集或无关完整测试，未修改正式资料/授权或重启正式服务。
