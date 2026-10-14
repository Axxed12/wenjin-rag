# 全局账号入口与私域目录登记（2026-10-12）

## 问题与约束

用户无法在常用页面找到退出/切换账号入口；AI Agents in Depth索引已就绪，却因目录将`private_config`排除在活动来源之外而无法进入授权资源列表。管理员凭据不能给普通账号共用。本轮不改真实原文、索引配置、身份库或既有授权，不重启运行服务，不自动导入或授权。

## 当前事实与选择语义

只读检查见[修前结果](private-readonly-before.json)和[检查脚本](inspect_private.py)：私域独立目标与schema1路由声明相同target，快速检查通过，完整`admit_index`为PASS；修前目录为`searchable=true/is_active=false`。私域尚未进入release，当前release活动库为osgearth。检查只读文件和Qdrant，没有实例化管理器或IdentityStore；不输出正文、查询或密钥。

旧私域服务版本以schema1路由中的唯一目标声明为准。裸目标是候选，不能仅因存在配置而生效；多目标、冲突声明、失效资产仍拒绝使用。这里的旧路由选择不等于新版release的准备/切换操作；已迁移资料库仍由release活动指针权威决定，缺失指针不回退旧配置。目录快速检查不代替检索/取件完整准入。

## 实现与验证

本轮由GPT 6 Luna子Agent分别处理后端、前端和独立权限核查，主Agent复查并重跑最终定向组。

- 后端：`retrieval/legacy_catalog.py`区别显式私域路由与候选，重复一致声明去重、冲突或多目标拒绝活动；`management_http/identity_http.py`管理目录允许admin发现元数据，附`readable`标记并禁用未授权检索；reader/MCP目录继续按活动资源与即时授权过滤。资源同步消费活动且有效来源，不生成grant。详情见[后端报告](backend-report.md)。
- 前端：顶栏账户菜单提供切换/退出；旧Bearer模式提供连接/断开。发出logout请求后立即清空状态并推进请求代次，不等慢响应。私域显示可读名称及发布/检查状态；管理员无读取权的目录保留，但禁用检索，撤权时清空旧证据且丢弃迟到响应。详情见[前端报告](frontend-report.md)。
- 凭据：用户可自建；已有管理员代签发API仍绑定目标账号。隔离HTTP合同证明首次改密门槛、reader身份不变、无权拒绝、授权可查、撤权新请求拒绝；未新增代签发UI。详情见[独立报告](validation-report.md)。
- 真实资产修后只读结果见[修后检查](private-readonly-check.json)：完整准入仍PASS，`private_config`来源为`published/is_active=true/searchable=true`；这不代表已授予任何账号权限，也不代表旧运行进程已加载。

主Agent最终命令与结果：

```text
uv run --no-sync python -m pytest tests/test_private_catalog_identity.py tests/test_admin_issued_reader_mcp_contract.py tests/test_admin_snapshot_consistency.py tests/test_legacy_search.py tests/test_identity_http.py tests/test_mcp_identity_http.py -q
14 passed（最终复跑6.47秒，17项依赖弃用警告）
node --test tests/admin_identity_auth_behavior.test.mjs tests/admin_frontend_catalog_refresh.test.mjs tests/admin_frontend_upload_cancel.test.mjs
30 passed
node --check src/wenjin/file_management/admin/app.js
node --check src/wenjin/file_management/admin/identity.js
uv run --no-sync ruff check src/wenjin/retrieval/legacy_catalog.py src/wenjin/management_http/identity_http.py tests/test_private_catalog_identity.py tests/test_admin_issued_reader_mcp_contract.py tests/test_legacy_search.py tests/test_identity_http.py
All checks passed
git diff --check
exit 0（仅既有工作区CRLF提示）
```

私域合同用临时root、内存Qdrant和确定性16维embedder创建真实BM25/向量索引；覆盖管理员发现/显式授权、有权用户目录/检索/正文、无权目录隐藏/正文拒绝、MCP个人身份桥、撤权后新请求403，以及裸配置、格式无效路由、多目标/冲突、collection失效与迁移后指针丢失。MCP桥与既有身份合同不等于本轮真实TCP状态化协议或GPU检索验收。快照合同覆盖查询中途目标移除或原文变化的拒绝。

失败处理：新私域测试初稿先因BM25路径类型/临时root约束失败，改为临时root下绝对Path；旧legacy目录测试简化target缺少当前目标契约字段，补为完整IndexTarget、保留原断言；旧身份HTTP测试精确两字段响应未适配已有session_policy，补实际1800/43200策略断言后通过。独立探查另发现`test_identity_store.py::test_web_and_mcp_tokens_are_separate_and_both_expire`仍期待ValueError，当前身份层映射AuthenticationFailed；本轮没有改该既有断言或声称完整测试套件通过。

后端子Agent另跑发布domain与既有stateful TCP回归，24项通过，详见后端报告；不与主Agent14项相加。目录变化：新增本轮日志/只读核查、链接检查脚本及结果、两个隔离Python合同；既有Node合同增加登出与私域撤权情景。更新`code.md`、管理页说明与日志索引；[本地链接检查](link-check.json)为135项、缺失0，`code.md`62行。

## 运行服务加载

本轮只读核实管理18777与MCP18765的监听进程及命令，见[运行进程](runtime-processes.json)。两者来自当前仓库、共用`data/file-management/manual-agent-identity.sqlite`。进程快照仅用于本轮定位，PID以后可能变化；没有读取或修改账号、授权数据。

静态资源按请求从磁盘读取，刷新管理页可取得新版HTML/JS/CSS；Python后台没有热重载，当前进程尚未加载本轮后端。加载时先用`Get-NetTCPConnection`和`Get-CimInstance Win32_Process`重新核对端口、模块、root与身份库，再停止这两个已核实的管理/MCP进程，保持模型/Qdrant与数据不动，然后从仓库根执行：

```powershell
uv run --no-sync python artifacts/iterations/2026-10-12-manual-agent-service/start_services.py
```

该[历史启动脚本](../2026-10-12-manual-agent-service/start_services.py)会复用仍在监听的进程，直接重跑不会重载后端；因此先停止已核实的两个后台进程。脚本会确保既有模型容器启动，并保持原远程模型配置和身份库路径。启动后可运行同目录`verify_services.py`做健康检查，但其无凭据检查不代表本轮权限/业务验收。管理员登录后打开授权页，目录同步登记有效私域资源，再由管理员显式授权；不得重做bootstrap、换身份库或给所有用户自动授权。

## 限制

真实库只做资产与选择状态核实，权限操作用隔离资料与账号完成；当前运行服务未重启、真实账号授权与浏览器视觉验收未做。保留用户真实数据和现有授权。

MCP新私域权限合同针对管理HTTP桥`/api/mcp/*`，不是原生`search_docs`私域路由接线。当前`mcp/runtime.py:create_router`仍只构造默认公开路由/OSG；未进入release的私域目标不会仅因管理目录登记就自动加入原生工具路由。本轮完成管理检索与正文权限链路，原生MCP私域目标接线及真实客户端检索仍是后续工作，重启本身不会补齐此能力。
