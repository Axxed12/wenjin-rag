# 轻量 GitHub Actions

日期：2026-10-17。接手main为`b976a62`，已跟踪工作区干净，无既有`.github`目录。已读AGENTS.md、PROJECT.md、code.md、pyproject.toml、uv.lock、tests/conftest.py和候选测试实际源码；主环境/真实资料/服务不变。GPT-6 Luna high开发与独立测试，主Agent审查和提交。

## 最小方案与选择依据

- 一份`.github/workflows/ci.yml`，PR、main push和手动触发；`ubuntu-24.04`单job，15分钟超时、`contents: read`、同ref取消旧运行，无部署/服务/密钥步骤。checkout不持久保存凭据，关闭uv缓存，避免不必要的Action和缓存写入。
- Python严格使用项目要求的3.12；uv固定0.11.3，与本机版本一致且官方release存在。`uv sync --locked --no-default-groups --group dev`安装主运行依赖及dev，排除默认ml组，不改pyproject/锁文件。测试`uv run --no-sync python -m pytest -q --strict-markers`后逐条列文件，不使用无筛选pytest或`-m quick`。
- 依赖审查发现`test_access_and_paths.py`虽为quick，后半会读取仓库真实catalog/manifest/config；整模块排除。`test_model_api_admission_contract.py`含TCP listener和模型/MCP服务构建；不纳入首轮。`test_frozen_dataset.py`及任何冻结集读取/生成/评测入口完全不收集；GPU/模型权重、真实索引、Docker、浏览器和外部服务合同均排除。

| 显式文件 | 实际输入 / 依赖边界 |
|---|---|
| `tests/test_audit.py` | StringIO、tmp_path日志，无实际用户调用记录 |
| `tests/test_bm25.py` | 临时SQLite FTS5、合成Chunk；jieba包内字典，无下载 |
| `tests/test_chunking.py` | 内存文档与行位置 |
| `tests/test_content_hashing.py` | 合成字节、临时原文及manifest，显式临时raw根 |
| `tests/test_evidence.py` | 字面JSON和注入fake OpenAI client，无真实生成API |
| `tests/test_fusion.py` | 内存排名列表，纯融合函数 |
| `tests/test_query_expansion.py` | 字面查询与静态规则 |
| `tests/test_query_format.py` | 结果字典的字符串格式化，不执行CLI主函数 |
| `tests/test_evidence_threshold.py` | 内联混淆矩阵；只调用纯指标函数，不执行evaluation main/读题集 |
| `tests/test_index_registry.py` | 临时manifest/SQLite/registry、FakeQdrant，无外部Qdrant |
| `tests/test_context_selection_contract.py` | 内存候选字典，预算、逐字保留及来源身份契约 |
| `tests/test_long_line_evidence_contract.py` | 合成长行、Chunk和上下文选择，无真实规范原文 |

上述仅是12个已经审过的文件，不等同quick标签全体；后续往这些模块增加测试时仍须审核实际I/O。Ubuntu选择依据是纯Python/锁定Linux wheels、SQLite FTS5和无Windows专用依赖的选定路径；首次托管Ubuntu运行前不宣称Linux执行通过。

## 实际核验与问题处理

- 官方GitHub refs API核实checkout v5.0.0与setup-uv v7.0.0的完整提交，两者对应action.yml为Node24；版本、源链接及SHA见[action-versions.json](action-versions.json)。只读公开API，不登录GitHub、不读取私人凭据。初始GitHub CLI未登录，网页读取网关403，改用无认证官方公开API完成核验。
- 独立本地工具actionlint 1.7.7使用官方release校验和核对zip后执行；不是新增CI依赖。`actionlint -no-color .github/workflows/ci.yml`退出0、无输出，见[static-check.json](static-check.json)和[工具身份](static-tool.json)。
- 独立测试在TEMP干净源码副本与新venv安装并运行相同命令，详细命令、输出和环境以[test-log.md](test-log.md)为准；主项目虚拟环境不作为轻量依赖证明。
- 本地等价结果：Windows Python3.12.3/uv0.11.3，全新轻量环境运行固定12模块，`60 passed, 4 warnings`、退出0，用时12.84秒；警告为jieba现有的invalid-escape与pkg_resources弃用，不删除断言、不吞错、不加skip。ML包审计未安装torch、transformers、sentence-transformers、huggingface-hub及HuggingFace embedding集成；jieba缓存与空NLTK_DATA位于TEMP，不使用主项目缓存/真实资产。
- pyproject.toml、uv.lock和tests/conftest.py保持原样，uv.lock SHA256为`1f32c3cea80ea944795e760c4029bc6bc3232717766c4f7dce3bd4c3bf15a3c9`。本轮只新增工作流/证据并更新code与索引，不改业务实现或既有测试。
- 首次临时源码导出发生递归复制，已停止导出进程PID 40928并确认进程退出，随后重新建立独立v2副本完成上述验收。v1残留位于`C:\Users\Onebiter\AppData\Local\Temp\rag-project-github-actions-20261017`；清理调用被自动审批拒绝，返回`rejected: blocked by policy`（更长包装错误已截断，无法恢复），未换方法绕过。v2源码/环境及静态检查工具留在TEMP供复核，均不纳入Git；没有创建服务或凭据。
- 最终文档检查165个本地链接均存在，code.md为88行；工作流摘要与静态检查时相同，见[final-check.json](final-check.json)。本轮提交仅包含CI及本轮证据/文档，原有未跟踪运行数据保留且不暂存。

## 当前边界与远端查看

**待首次远端验证。** 本轮不推送、不改仓库设置、不触发托管运行、不部署/重启服务。Windows本地等价通过不能替代GitHub Ubuntu runner、Windows Docker、GPU、浏览器或生产验收。

用户首次推送包含此文件的main或提交/更新PR后：打开GitHub仓库 → Actions → CI → 对应commit的运行 → targeted-tests → Install locked test environment / Run targeted synthetic tests查看日志。手动运行需工作流文件已在远端默认分支中，再从Actions → CI → Run workflow选择分支。失败时反馈运行链接、commit SHA和失败step日志，去掉可能的凭据信息；不要只反馈本地pytest结果。

官方操作依据：[手动运行工作流](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)。首次远端的Action下载、Ubuntu依赖安装、Python/SQLite及网络镜像可达性仍待该运行确认；不修改仓库设置来掩盖失败。
