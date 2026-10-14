# MCP入口管理与容器控制审核

日期：2026-10-14。基线main2674e1d，独立开发分支codex/mcp-entry-control。业务和独立测试仍由GPT-6 Luna high分工，root审核与交付。

## 用户边界

用户要求两类管理：选择新旧默认连接入口/配置，以及停止或启动既有新旧MCP容器；另一个对话负责前端和部署。本轮不实施真实启停或部署。默认配置不迁移已经连接的Agent，不自动启停。旧入口当前为原生Python，Docker控制接口不得冒充可控容器或停止该进程。

管理API强制账户模式管理员Cookie、同源Origin/CSRF；控制broker另用服务间secret，只有broker挂Docker socket。允许集合只有配置固定Compose项目中的mcp/mcp-stateless服务，不能操作管理/模型/Qdrant、任意其他项目，不接受client指定Docker命令或容器名。操作仅启动/停止已创建容器，不能创建/构建/删除或自动部署。

## 第一批审核问题与处理要求

- 初始freeze16个自有文件聚合hash为4a9df9ad…c32c0e，18项新合同与8项旧合同是该版本历史范围。unknown错误码曾被测试放宽为in_progress或outcome_unknown，root要求恢复严格契约，不能以修改期望替代实现修正。
- 实际源码审读发现async管理handler直接调用同步HTTP/SQLite，最长60秒阻塞eventloop；移入线程池并验证并行健康请求。
- 硬编码项目wenjin不适用于已有local/cloud部署；改为启动时必配的固定project，并按固定service标签重查目标/唯一性，HTTP请求不能改变范围。
- paused/restarting不等于已停止；start/stop不得提前报告目标状态，动作后重查真实inspect/generation，不确定结果隔离该入口直到复核。
- 超时分支active映射优先于uncertain，返回错误分类含糊；要求明确unknown，同时保留互斥。
- SQLite contextmanager不会自动close连接，要求显式close，避免Windows句柄残留；JSON请求有界、类型与extra fields校验、安全中文错误。
- 内部控制HTTP禁止系统proxy/redirect并验证broker地址；控制服务不可达时连接元数据仍可查看，状态明确不可控，不能伪报运行/停止。

所有修正集中受控解冻一次，测试等待新freeze后定向验证；不触碰真实服务、数据库或Docker Engine，不扩展前端/任务中心。

## 最终审核与交付

- 独立测试最终29项新增合同及8项相关回归通过；历史18项不能作为最终版本证据。业务Python六文件聚合SHA-256为`24181f1989a5a4906d62afe439b7f17395af213924eaf9b1850577f7de1226ab`，配置收尾未改变业务源码。
- Root发现Docker healthcheck不能继承entrypoint为broker进程导出的secret环境；配置改为读取固定secret文件。独立测试实际执行合成secret/fake HTTP健康命令通过，静态变量/依赖检查通过；未执行Docker/Compose。最终overlay SHA-256为`c61b348bc6089d87fea5876ed0d6b5016d4928bbb67fe3c1e26f07aa079402d3`。
- 操作轮询只读状态；不确定结果由入口列表GET触发真实状态复核，前端需调用该接口。容器running不是协议健康；当前原生旧入口不在容器控制范围。
- 测试服务已关闭，合成pytest目录默认保留；没有使用真实凭据、Docker socket或业务数据，没有部署、推送、重启服务。主目录已有未跟踪运行文件和worktree既有synthetic-only均不纳入提交。
- 本轮选择性保存源码、配置、接口说明、测试及脱敏记录，以ff-only交付main；如main发生变化则停止并重新核对，不能覆盖主分支。

交付结果：实现提交`10288be1818e6c54c711ca867f54d147d9238b04`已从基线`2674e1d`以`--ff-only`合入main，无冲突。暂存检查发现两个新增Python文件尾部多余空行；开发Agent仅修剪EOF空白并通过定向Ruff，没有改变执行代码，测试hash对应修剪前源码，详细hash见development-log。最终暂存区diff检查、四份新文档本地链接检查通过，code.md为80行。main已跟踪文件无未提交修改，原有未跟踪运行文件不动；未推送或部署。独立工作区及来源分支保留。
