# 无状态MCP整合交付

日期：2026-10-14。

## 基线与实际合入

- 主目录原main：90a7e1f（本轮检查确认最新代码、配置、provider和必要文档已经保存，没有新的未提交源码需快照）。来源：codex/mcp-stateless-20261009 / ab53e7f。
- 集成分支：codex/integrate-mcp-stateless，独立worktree从上述main起点创建。f0e200e对应21cebfd，ab53e7f对应20f50b7；适配39d348e；文档/历史单文件5c2a9f2、a5acdc4、e51269a；独立测试/审核a5341d0。
- 没有移植842f1a2、0f506a2、91dd611。仅按字节补入历史handoff-snapshot.json，没有复制旧源码。
- 合入前main没有新提交或tracked改动，incoming新增文件不存在与本机未跟踪文件的碰撞。主目录实际执行git merge --ff-only codex/integrate-mcp-stateless，main已从90a7e1f快速前进至a5341d0。本报告之后的收尾提交只更新交付记录，同样通过ff-only纳入main；最终HEAD以git log为准。

## 冲突与兼容

code.md、docs/development-log.md有冲突，以main最新内容为底追加新版条目，未整份采用旧文档。基线providers.py、identity/store.py、managed_client.py、settings.py、模型准入、旧MCP工具以及主pyproject.toml/uv.lock均保留；src差异只新增三个stateless模块。

新版适配当前FastMCP ToolResult，保留错误标识/安全中文详情，翻译协议与下载路径；Compose接入模型90/5/5、队列8/30、管理桥210/5/5配置。个人凭据/ACL逐请求读取，版本和来源校验复用最新业务实现；query_only与WAL协调保留。新版独立依赖/进程，旧2025-11-25入口和管理登录会话保留，不追溯截止日前能力。

## 本轮证据与边界

独立新版组合16项及6项子测试通过、0skip；受影响旧环境8项通过。官方TypeScript SDK2.3.1实际loopback discover/list/call、授权撤回与凭据撤销、shared/managed版本取件及原文SHA、新旧SDK入口隔离通过。新版HTTP实际工具调用测试以合成后端注入真实ModelProviderError，队列错误具有isError/structuredContent。WAL sidecars下query_only拒DDL和初始化，读段主DB SHA不变。详见[test-development-log.md](test-development-log.md)。

本轮仅合成输入、临时身份库/索引/服务；独立Python3.12.3环境为FastMCP4.1.0/MCP2.3.0，旧主环境3.4.4/1.29.0未升级。未构建新镜像，未执行容器/HTTPS、真实模型/GPU、真实语料、冻结评测或生产验收。2026-10-09同类结果是历史证据，不代表集成版通过。

## 保留状态

来源分支及worktree保留，集成worktree保留；没有推送、部署或重启真实服务。原目录已有未跟踪运行数据/历史产物保持原状，不纳入本轮提交。

合成测试服务/客户端已退出，隔离依赖.venv/node_modules忽略保留。pytest默认保留最近tmp_path：本轮1129/1130/1131各有一个合成identity.sqlite，没有真实资料，未提交、未删除共享临时目录。集成worktree误生成的91字节synthetic-only仅含临时CA文件路径；自动审批拒绝精确删除（blocked by policy），保留为未跟踪文件，未绕过策略或提交。

基线已有部分历史artifact链接未纳入Git，因此在新worktree缺失；本轮新增链接检查有效，此债务单列，未伪造历史证据。下一步如需要容器/HTTPS或真实模型联验，另开明确批次重新构建并绑定当前源码/镜像身份。
