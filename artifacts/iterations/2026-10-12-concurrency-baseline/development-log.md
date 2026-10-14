# 第一轮并发基线（2026-10-12）

## 范围与执行约束

本轮以当前源码为准，仅新增隔离测试程序、配置和记录；不优化业务实现，不引入Redis/缓存，不运行冻结评测集或无关完整回归。主Agent协调审核；GPT-6 Luna high分别开发测试程序和独立验收。并发由程序产生。

已阅读AGENTS.md、PROJECT.md、code.md、docs/dual-deployment.md及环境说明。历史2026-10-08部署产物只用于定位可复用镜像/流程，不是本轮实测。当前HEAD见[本轮资源检查](preflight.json)，实际运行还须记录源码hash与镜像身份。

## 计划与停止条件

- 独立随机Docker项目、网络、Qdrant和临时root/账户/凭据；不挂载真实资料、索引或身份数据库。全部测试端口仅127.0.0.1；为宿主程序准备fixture，模型和Qdrant也绑定随机loopback端口。数据网络internal，客户端连接另用bridge网络。
- 一个GPU模型服务，现有两个0.6B权重目录只读挂载；缓存依赖镜像必须装配当前源码，不能把镜像日期当源码身份。
- 单请求端到端先行；冷启动、首次重排与预热分开；共享资料库、受管文件分别按1/2/4/8并发，每档固定16请求。每客户端独立MCP2025-11-25 initialize/initialized/能力协商/session。
- 超时60秒；OOM、GPU空闲<512MiB、Windows可用内存<2GiB、失败率>=5%或明显过载时停止增加负载并保存证据。
- 统计全部请求及成功子集延迟，P95注明小样本经验分位；记录HTTP/MCP/下游503/超时。排队时间不可用时写“未测得”。资源为采样值，不声称覆盖瞬时峰值。
- 少量受控请求验证两身份、替换、撤权、删除及取消；操作前已开始与操作完成后新请求分别记录，未观察到重叠不得声称竞态通过。

## 当前核查

`preflight.json`为本轮实时读取：4070 SUPER 12282MiB，初查1879MiB已用；Windows可用约7.7GiB，Docker分配约15.2GiB/12CPU。既有Qdrant、MinerU、Dify与其他项目保留。

源码显示RetrievalService.search持锁覆盖整条检索；ModelAPI inference_gate忙时直接503。此为静态候选瓶颈，待实际结果证实。同步线程取消不能自动视为推理中止。独立验收要求见[验收方案](independent-acceptance-plan.md)。

## 本轮执行证据

有效实测已完成，详见[最终报告](final-report.md)及[独立验收](independent-acceptance-report.md)。业务源码三服务与宿主hash均为`6e9b9c316e0ac2a50355b432d57502365f3218a4ba2fb63b0759517ab996658a`，未改业务实现。最终离线检查6项通过（MockTransport及字段合同，不是业务HTTP验收），见[离线证据](offline-harness-check-results.md)。

主Agent执行 `uv run --no-sync python artifacts/iterations/2026-10-12-concurrency-baseline/run_concurrency_baseline.py prepare` 首次失败：overlay YAML重复management键，`docker compose ... config --quiet`报 `mapping key "management" already defined`。只生成临时配置/state，未启动容器或发送检索。开发Agent修测试配置后再验证，不能将此算为业务失败。

后续失败分别保留在runs：064619 internal-only网络阻断宿主访问（模型/Qdrant启动正常，改隔离测试网络）；065110 reader_token绑定过晚；065621 evidence误读target_id而实际schema为index_target_id；070154统计字段start_utc与wire_start_utc不一致。均为测试程序问题，各项目已清理，未将这些结果混入有效矩阵。配置与失败JSON/log按各run保存，详情见[test-development-log.md](test-development-log.md)。

## 实测结论与命令

仓库根执行：`uv run --no-sync python artifacts/iterations/2026-10-12-concurrency-baseline/run_concurrency_baseline.py prepare`，随后使用该命令返回的临时state路径执行`run --state <state路径>`。有效批次070604完整矩阵；071733使用`run --state <该批state路径> --controls-only`，只补正确性，未重跑矩阵。state含临时凭据不保留；每批脱敏manifest、overlay、resolved-compose、requests、resources、服务日志及cleanup保留。各级依赖按单请求→预热→1/2/4/8推进；没有资源/OOM停压，混合碰撞失败后未进一步扩压。

- 070604：128/128矩阵成功，另4条单请求/预热成功。并发8共享7.509请求/秒、P95 1.378秒；受管3.534请求/秒、P95 2.437秒。受管4→8吞吐增约4%，为首先出现的平台。排队时间未测得。
- GPU采样最高5356MiB/41%，宿主可用内存最低6623MiB；44采样实际间隔中位3.562秒，不能称瞬时峰值。服务CPU/内存逐项见最终报告。
- 一对跨链路请求1/2成功，下游模型与管理桥503、MCP外层200工具失败；记录忙拒绝候选原因，不修改业务实现。
- A/B授权对照通过；071733替换、撤权、重授权、删除后新请求通过。单切片fixture无法证明多切片版本一致性；操作前请求只证实可能重叠。
- 两批取消均复现MCP `Request already responded to`异常；原请求返回Request cancelled，新会话成功。线程中止与单请求资源即时释放未测得，真实HTTP超时未验证。summary总体状态的测试汇总缺陷与业务异常分开解释。
- 全部本轮临时项目/数据/账号凭据已清理，初查7个运行容器ID及状态保留，见[postflight](postflight.json)。最终交付脚本hash、源码未变、脱敏及文档链接检查见[delivery-checks](delivery-checks.json)。

## 下一轮与目录变化

优先查跨进程模型准入/错误语义，其次取消会话双响应，再补管理锁与桥接等待观测；完成边界后增加代表性多切片语料及重复稳态。没有Redis、语义缓存或检索调参，本机Docker结果不是内网/生产容量验收。

新增本iteration测试程序、controlled helper、离线合同、脱敏runs、独立验收与报告；更新code.md当前结论和docs/development-log.md索引。最终脚本未在每次历史run开始前冻结，最终hash仅标识交付版本，不能追溯替代历史执行脚本身份。
