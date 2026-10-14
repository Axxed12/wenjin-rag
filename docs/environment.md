# 项目开发环境

> 维护说明（2026-10-07）：以下命令从项目根目录执行。文中的旧实测数字只代表标注日期；本文搬迁未重跑环境、构建或验收。当前能力与未验证项见 [当前状态](../code.md)。

适用：十多人的团队场景，本机开发、内网单机服务。这里记录环境准备；不表示系统已完成生产验收。

## 2026-09-15 实测结果

`uv pip check` 通过，153 个已安装包依赖兼容。`uv run python scripts/check_environment.py --models` 全部通过：GPU 实际运算、内存 PDF 双解析器读取、SQLite FTS5、Qdrant 写入和过滤查询、MinerU 健康检查，以及本地 0.6B Embedding / Reranker 推理。环境报告已在 FastMCP 调整后重新生成，当前记录为 FastMCP 3.4.4。

Embedding 输出 1024 维；重排正确区分了两条冒烟候选。这只是加载和调用正确性检查，不是 OSG 检索质量结果。原有 `ingest.py` 仍成功读出 29 份文档。

本轮模型加载出现 `torch_dtype` 弃用提示，但未导致检查失败；后续依赖适配时改用 `dtype` 并复验。Qdrant 的 HTTP API-key 提示来自本机 `127.0.0.1` loopback 开发连接，远程内网部署仍需TLS或网络隔离。

详细结果见 `artifacts/environment-check.json`，模型文件指纹见 `artifacts/model-assets.json`。Qdrant 拉取镜像摘要为 `sha256:12364fe851b9f17356fc88189fc06d1b521262e04659ec7345975b00c9246a10`。

## 为什么只用一个主虚拟环境

2026-09-16新增例外：`evaluation/ragas/`为独立uv评测项目。实际解析确认RAGAS的instructor依赖
与主环境`openai>=3.8.0`不兼容，所以隔离评测环境。RAGAS锁定0.4.3（2026-01-13发布），
langchain-community锁定0.3.27、langchain-openai锁定0.3.28；全部评测依赖使用
`exclude-newer=2026-07-15T23:59:59+08:00`。主环境依赖配置不变。

```powershell
uv sync --project evaluation/ragas --locked
uv run --project evaluation/ragas --locked python evaluation/ragas/check_install.py
```

已验证关键指标导入和ID指标实际计算；501个发行文件时间均满足截止日；不读取.env或调用模型。
独立环境97个包依赖兼容；主环境153个包兼容、62项测试通过。使用`uv sync --project evaluation/ragas --locked`精确同步，避免旧适配器的残留依赖影响检查。
完整安排见 [evaluation/PLAN.md](../evaluation/PLAN.md)。本检查不代表OSG的RAGAS效果评测已完成。

主项目由 uv 管理 `.venv`、`pyproject.toml`、`uv.lock`。固定切片、父子切片、不同候选数、BM25、dense、RRF、reranker 的实验共用环境，通过配置和独立结果目录区分。

MinerU 依赖独立，复用已有 Docker 容器，不安装进主环境。Qdrant 作为独立服务容器运行。仅当某个实验要求不兼容的 Python、PyTorch 或解析库版本时，才创建单独 uv 项目及锁文件；不是每个算法都建一个环境。

依赖分组只是同一环境中的依赖集合，不是隔离机制。对照实验除了固定环境，还要固定语料、模型文件、问题集和除被测因素以外的参数。

## 已配置的组件

| 部分 | 配置 |
|---|---|
| Python | 3.12 系列；当前解释器为 3.12.3 |
| 主框架 | llama-index-core，附 HuggingFace 与 Qdrant 集成 |
| 普通 PDF / 表格 | PyMuPDF、pdfplumber |
| HTML | BeautifulSoup、lxml |
| 扫描件及复杂版面 | 现有 mineru-api 容器，实测 MinerU 3.4.5 |
| Embedding / reranker | sentence-transformers、Transformers、CUDA PyTorch |
| 关键词检索 | Python SQLite FTS5，环境脚本实际建表验证 |
| 向量服务 | qdrant/qdrant:v1.19.1，独立 Compose 项目 |
| MCP | FastMCP 3.4.4 + MCP SDK 1.29.0；目标协议2025-11-25状态化生命周期 |
| 模型接口及配置 | OpenAI 兼容客户端、HTTPX、pydantic-settings |
| 开发检查 | pytest、ruff |

完整已解析版本以 uv.lock 为准。PyTorch 2.11.0 使用官方 cu128 wheel 索引，索引设为 explicit，仅供明确指定的 torch 使用；利用本机已有的 wheel 缓存。选择的是可验证的版本组合，不追求每个组件最新。驱动显示的 CUDA 能力版本与 wheel 自带运行时版本不是同一个概念。

## 常用命令

在本项目目录执行：

```powershell
uv sync --locked
docker compose up -d
uv run python scripts/check_environment.py
uv run python scripts/check_environment.py --models
uv run python ingest.py
uv run ruff check ingest.py scripts
```

无需激活虚拟环境。`--models` 会顺序加载 Embedding 和 Reranker 做小样本推理，执行后释放模型；不会运行 4B 模型，也不会提交 MinerU OCR 任务。

脚本写入 `artifacts/environment-check.json`。最近一次执行会覆盖报告；不带 `--models` 的报告不含模型检查，不能据此声称模型已验证。

## 本地配置

`.env.example` 是模板；`.env` 是本机配置，已设置随机 Qdrant API key，禁止提交。不要在终端打印完整 `.env` 或带变量展开的 Compose 配置。

MCP审计默认用`WENJIN_AUDIT_SINK=stdout`输出单行JSON，交给Docker日志驱动；
`WENJIN_AUDIT_INCLUDE_QUERY=0`只保存Query哈希和长度。无集中日志平台时可改为`file`并将
`WENJIN_AUDIT_PATH`指向持久卷；`artifacts/audit/`不提交Git。

现有模型位于：

- `E:/Models/Qwen3-Embedding-0.6B`
- `E:/Models/Qwen3-Reranker-0.6B`

4B 两档也存在于 E:/Models，但本轮不加载。不同尺寸的 Embedding 必须建立各自匹配的索引，不能用 4B 文档向量配 0.6B 查询向量。Reranker 不落库，可以对固定候选分别打分。

环境检查在导入模型库前开启 HF_HUB_OFFLINE 和 TRANSFORMERS_OFFLINE，加载时使用 local_files_only。`.env` 不会自动变成所有 Python 脚本的环境变量：后续业务入口也必须显式读取配置。当前已验证的本地加载不等于整机已完成断网测试。

## Docker 服务与数据

- Qdrant：`http://127.0.0.1:6333`，API key 读取 `.env`；6334 为 gRPC。
- 数据卷：`wenjin-rag_qdrant-data`。
- MinerU：`http://127.0.0.1:8000`；健康接口 `/health`，API 文档 `/docs`。
- MinerU 已有容器不纳入本项目 Compose。已有 Dify 服务也不归本项目管理。

停止本项目服务可执行 `docker compose stop`；重新启动用 `docker compose up -d`。不要运行 `docker compose down -v`，它会删除本项目数据卷。

环境脚本在 Qdrant 中创建唯一的临时测试集合，验证写入和过滤查询，并在 finally 中删除该集合，不操作业务集合。

当前 Qdrant 只绑定 loopback。MCP 已实现 Streamable HTTP 服务并在本机完成真实会话、工具调用和取件检查；目标协议保持 `2025-11-25` 状态化生命周期（initialize、notifications/initialized、能力协商及可选会话ID）。本机真实服务与本机ACL检查不等于内网远程、多用户IAM或生产可用性验收。以后其他内网机器应访问 MCP 服务，由 MCP 服务访问数据库；不必直接向所有 Agent 开放向量库端口。

Docker Desktop 启动时，原有 restart policy 可能自动启动 Dify 和 MinerU。注意总内存；运行 OCR 与模型实验时尽量错峰，不要把虚拟环境隔离误认为显存隔离。

## 实验产物怎么隔离

未来每次实验保存配置、语料清单哈希、切分配置、模型标识、锁文件版本和指标。不同切分或 Embedding 的索引使用不同 collection / 索引版本；结果目录使用独立 run_id。不要覆盖一份索引后拿旧指标和新结果混合比较。

## 离线交付边界

当前准备的是 Windows 开发环境。后续交付内网时，还要根据目标操作系统和 Python 版本准备对应 wheel、模型目录、容器镜像及校验清单，并在无公网环境复验。

`uv.lock` 记录依赖解析结果，不包含安装包；`uv sync --offline` 只在所需包已缓存或可从本地来源取得时才成功。Qdrant 快照也不能代替原始语料、关键词索引及配置备份。

已完成 Windows 本机隔离环境中的状态化 MCP 生命周期、`/files`原始字节Hash、ACL拒绝和 registry 切换/重启/回滚/重启检查；服务进程已终止、端口已关闭，原active registry未变。该演练不是 clean offline 安装、原生 Linux、目标内网远程调用、企业 IAM、生产可用性或生成模型后端验收。待后续验证：真实扫描件质量、4B 推理、完整断网启动、目标平台依赖与镜像交付。环境冒烟检查不宣称这些已经完成。
