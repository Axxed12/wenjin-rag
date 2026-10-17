# GitHub Actions 轻量测试集独立验证

日期：2026-10-17。源码基线：main `b976a623cb1de6f24f5fdd375907d340ed2991fe`。验证为本机 Windows 合成合同测试，不代表 Ubuntu、Docker、GPU、浏览器、GitHub Actions 或生产验收。

## 隔离环境

- 主仓库：`E:\日常任务\rag-project`；主 `.venv` 未修改。
- 临时源码副本：`%TEMP%\rag-project-github-actions-20261017-v2\source`；临时环境：`%TEMP%\rag-project-github-actions-20261017-v2\venv`。
- Windows 11 专业版 10.0.26300，Python 3.12.3，uv 0.11.3。
- 源码副本由 Git 跟踪的 Python 源文件、pyproject.toml、uv.lock、.python-version、tests/conftest.py 和 12 个批准测试组成；未复制 .env、语料、数据库、索引或配置运行资产。临时副本不含 .env 或 corpora。
- 运行环境设 `UV_NO_ENV_FILE=1`、`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`、`PYTHONNOUSERSITE=1`、`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`；测试 TEMP/TMP、NLTK_DATA、HF_HOME、XDG_CACHE_HOME 均指向该任务独立临时目录。NLTK_DATA 中没有文件。
- `test_bm25.py` 会触发 jieba 惰性初始化，读取安装包自带词典并把 jieba.cache 写入独立 scratch；没有下载模型或访问外部服务。SQLite FTS5 与临时文件测试在本机 Windows 可用。
- 两个新增合同测试只用内存 dict/string；导入 select_context 与 chunking 路径未加载ML包。

## 安装

在临时源码副本目录执行，项目环境路径设为临时 venv：

    uv sync --locked --no-default-groups --group dev

退出码：0。锁解析 177 个包，安装 140 个包。随后验证 python 与 wenjin 导入路径均在 TEMP 副本/venv。ML 包审计退出码：0；torch、torchvision、torchaudio、transformers、sentence-transformers、llama-index-embeddings-huggingface、huggingface-hub 均未安装，torch、transformers、sentence_transformers、huggingface_hub 均不可导入。审计原始输出见 [ml-package-audit.stdout.txt](ml-package-audit.stdout.txt)。

uv.lock 主仓库与临时副本 SHA-256 一致：`1F32C3CEA80EA944795E760C4029BC6BC3232717766C4F7DCE3BD4C3BF15A3C9`。

## 测试命令与结果

工作目录为临时源码副本。以下为 workflow 中相同的测试命令和 12 个显式路径：

    uv run --no-sync python -m pytest -q --strict-markers tests/test_audit.py tests/test_bm25.py tests/test_chunking.py tests/test_content_hashing.py tests/test_evidence.py tests/test_fusion.py tests/test_query_expansion.py tests/test_query_format.py tests/test_evidence_threshold.py tests/test_index_registry.py tests/test_context_selection_contract.py tests/test_long_line_evidence_contract.py

结果：`60 passed, 4 warnings in 12.84s`，退出码 0。warning 为 jieba 在 Python 3.12 下的 3 条 invalid-escape SyntaxWarning，以及 pkg_resources deprecated UserWarning；无测试失败。原始 stdout 在 [pytest-allowlist.stdout.txt](pytest-allowlist.stdout.txt)。

## 过程偏差与边界

第一次同步命令的工作目录误指向主 checkout；UV_PROJECT_ENVIRONMENT 已指向 TEMP，因此没有修改主 .venv，但 editable 包当时仍指向主源码。发现后在临时源码副本中重跑同一锁定同步，并核验最终 wenjin.__file__ 指向 TEMP 副本，然后才执行测试。第一次同步输出保存在 [uv-sync-superseded-wrong-cwd.stdout.txt](uv-sync-superseded-wrong-cwd.stdout.txt)；最终正确同步输出见 [uv-sync.stdout.txt](uv-sync.stdout.txt)。

首次v1导出发生递归复制，导出进程PID 40928已强制停止并确认退出；v1残留目录为`C:\Users\Onebiter\AppData\Local\Temp\rag-project-github-actions-20261017`。清理调用被工具拒绝，原文结尾`rejected: blocked by policy`，更长包装错误已截断无法恢复；未绕过重试。正式结果来自随后重新建立的v2副本。临时环境保留供复查，不提交到仓库；未创建凭据或服务。

workflow 的静态 actionlint 结果和 Action SHA 校验由主 Agent 独立完成，actionlint exit 0；本 Agent未重复该检查。当前验证只覆盖 Windows 本机与 12 个显式合成测试文件。Ubuntu 24.04 runner 和远端 GitHub Actions 首次执行仍待验证。
