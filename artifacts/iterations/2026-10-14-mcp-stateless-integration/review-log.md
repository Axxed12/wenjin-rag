# 无状态MCP整合审核

日期：2026-10-14。业务整合和独立测试由GPT-6 Luna high分工执行，主Agent负责协调/审核/Git交付。

## 基线与隔离

- 主目录main为90a7e1f，已保存模型准入、账户授权、管理凭据和前端后续开发。实际核对tracked diff/index为空，src/wenjin/models/providers.py与文档已跟踪；ignored下未发现遗漏的业务.py。未跟踪运行数据库/原文/实验产物继续留在本机，不纳入基线。
- 应用任务清单中前端和来源对话idle，无其他本树Agent写文件；整合期间持续核对main漂移。来源分支codex/mcp-stateless-20261009为ab53e7f，保留原worktree。
- 从90a7e1f创建独立worktree与codex/integrate-mcp-stateless分支；只选择f0e200e、ab53e7f（对应21cebfd、20f50b7），未整体移植842f1a2/0f506a2/91dd611。仅补必要历史handoff文件，注明历史来源。
- code.md与docs/development-log.md冲突以当前main为底追加新版条目；没有整份采用旧文档。旧2025-11-25服务与主依赖保留，新2026-07-28服务独立依赖，不追溯历史截止日前能力。

## 兼容性审核

- cherry-pick后核对providers.py、identity/store.py、tools.py、managed_client.py、settings.py、主pyproject/uv.lock及compose.base与90a7e1f无差异，未以旧快照覆盖后续开发。
- 新版旧适配层仅处理dict，但当前tools会返回FastMCP ToolResult传递模型排队/读取错误，必须翻译typed结果的协议标签/下载路径并保持isError和structuredContent。
- 新版Compose需显式接入当前模型/桥接read/connect/pool变量，避免只按默认值而忽略用户覆盖配置。
- query_only/WAL sidecar、逐请求个人凭据和授权、来源版本绑定及管理会话按最新实现核验。历史SDK/容器/HTTPS结果仅作为开发依据，不能充当本轮新源码验收。

## 待完成

业务适配、定向合同与实际SDK网络验证、记录/链接检查、最新main漂移复核和ff-only实际合入。若本轮不构建容器、不使用真实模型或未验生产，必须明示范围；不推送、不部署、不改真实数据。

## 独立验收与合入前审核

- 集成业务适配为39d348e；后续5c2a9f2/a5acdc4/e51269a仅文档/历史证据/结果记录。主Agent审读新增兼容合同及独立测试日志：新版16项、6项子测试、0 skip，旧环境受影响8项通过；包括官方TS SDK 2.3.1实际loopback discover/list/call、逐请求Alice/Bob凭据/授权撤回、版本取件/原文SHA、新旧SDK入口隔离。
- 实际register_tools.search_docs在新版HTTP工具调用中得到真实ModelProviderError队列满/超时对象（合成后端注入），返回isError=true、结构化错误和2026-07-28标签。不是仅对helper手构造对象的验证，也没有调用真实GPU。
- 临时WAL数据库有live sidecars时，当前StatelessIdentityStore启用query_only、拒DDL和initialize；只读段主DB SHA不变。旧登录/账户授权/MCP凭据与模型准入定向合同以main已安装旧依赖运行，导入本集成src，不升级主环境。
- 独立环境下载超时及新测试fixture漏QDRANT_API_KEY/io均有记录，修正测试后限定重验，未借机改业务源码。开发侧合成Compose静态JSON检查确认模型90/5/5、队列8/30、桥210/5/5；没有新镜像构建、容器/HTTPS、真实模型或生产验收。
- handoff-snapshot.json按来源ab53e7f字节恢复，blob相同，标记2026-10-09历史；没有导入旧models/provider或旧接手源码。旧历史链接债务保留说明，本轮新增链接已检查。
- 收尾纠正：pytest tmp_path并非自动清空，1129/1130/1131各保留一个无真实资料的合成identity.sqlite；未删除共享pytest目录，未提交数据库。合成测试服务/客户端已退出。依赖.venv/node_modules保留用于复验且忽略。
- Compose检查误生成91字节synthetic-only文件，内容只是临时CA文件路径；自动审批拒绝精确删除（blocked by policy），未换手段绕过，保留为集成worktree未跟踪文件，排除提交。主目录及来源目录无此临时文件。
- 当前main仍90a7e1f且tracked干净，其他相关对话idle；incoming新增路径无本机文件碰撞，core src/根依赖与基线一致。下一步提交测试/审核记录并实际ff-only，合入结果另行更新，不以计划代替已合入。
