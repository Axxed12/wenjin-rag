# 问津：内网研发资料 RAG / MCP 检索服务

把一个团队需要的公开技术文档，变成**每条都能溯源、可重建、可回退**的检索证据，再通过
MCP 交给编程 Agent 使用。

当前是**个人实现与学习项目**，只在本机完成验证，不代表任何公司、客户或生产环境部署，
也没有通过生产验收。

## 这个项目在解决什么

RAG 的难点通常不在“能不能检索到”，而在**检索到的东西能不能被信任**。本项目围绕这一点
做了三件事：

1. **可溯源**——每条证据都带上游 URL、固定版本、原始文件行号（PDF 为物理页码）和原文，
   调用方可以自己核对，而不是只能相信一个分数。
2. **可复现**——语料不提交到仓库，只提交 manifest：来源 URL、固定 commit、许可、逐文件
   `raw_sha256` / `canonical_sha256`。任何人都能按它把同一份语料取回来并校验，重建出同一套
   索引。索引本身是派生产物，不入库。
3. **可回退**——资料变更时新建独立候选索引，通过验证后原子切换 active 注册表，必要时一条
   命令回退；旧索引在验收通过后退役，避免“伪回退”。

## 架构

```text
外网公开资料
  → 固定版本 / 许可 / manifest / 逐文件双 Hash
  → 按组织流程摆渡入内网
  → 多格式解析（保持原始行号与 PDF 物理页）
  → 切片（fixed / blocks / blocks-split）
  → Qdrant dense + SQLite FTS5 BM25（中文经 jieba 切分，见"中文检索"）
  → RRF 融合
  → 本地 Qwen3-Reranker-0.6B
  → Top-K 原文证据（来源 / 行号或页码 / 版本）
  → 状态化 MCP 工具 search_docs
  → 客户端 Agent 依据证据作答
```

MCP **只返回证据，不生成最终答案**。RRF 和 Reranker 分数只是排序信号，不是答案正确率；
证据充分性由调用方判断。

## 语料

仓库只提交 `corpora/<corpus-id>/source-manifest.json`，**不提交语料原文**（`raw_data/`
被 Git 忽略）。原文按 manifest 从上游获取并逐字节校验。

| 语料 | 版本 | 许可 | 白名单文件 | 002 候选 chunk |
|---|---|---|---:|---:|
| OpenSceneGraph | 3.6.5 | OSGPL | 29/30 | —（active 为 307） |
| osgEarth | 3.8 | LGPL-3.0（含例外） | 13/15 | 44 |
| GDAL | 3.13.1 | MIT（部分组件另有许可） | 14/17 | 344 |
| PROJ | 9.8.1 | MIT（X/MIT 风格） | 9/12 | 63 |
| OGC 3D Tiles | 1.1 | OGC Document Notice | 2/2 | 1025 |
| OGC WMS | 1.3.0 | OGC Document Notice | 2/2 | 215 |

各语料的来源、固定 commit、许可和逐文件 Hash 见
[`corpora/`](corpora/) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

**已知缺口**：`scripts/fetch_corpus.py` 目前只支持带 `repository` + `commit` 的 OSG 风格
manifest（已实测自动下载 30/30）。其余 5 个语料需要按 manifest 里的逐文件 `source_url`
获取，尚无对应脚本。

## 本机快速开始

下列命令保留根目录的开发环境入口。新的本机完整Docker和云端轻量backend配置见[双部署操作说明](docs/dual-deployment.md)；云端拓扑目前是可运行配置模板与本机隔离模拟，不代表已购云主机或公网部署。

要求：Python 3.12、uv、Docker、NVIDIA GPU，以及本地 Qwen3-Embedding-0.6B 与
Qwen3-Reranker-0.6B 模型目录。

依赖解析默认走清华源（`pyproject.toml` 里的 `[[tool.uv.index]]`，`uv.lock` 中锁定的
也就是这套源）。这是为此项目在国内网络下的可复现安装设的；在境外网络下若解析缓慢，
把该条删掉或改用官方源重新 `uv lock` 即可。

**这是一个有意识接受的供应链取舍**：`default = true` 意味着全部依赖的解析都经过第三方
镜像，而不只是某一个包。当前的缓解手段是安装一律以 `uv.lock` 为准（CI 与文档都用
`uv sync --locked`），锁定的是具体版本与该源；若你的环境要求只走官方源，删掉这条并
重新 `uv lock` 即可，代码本身不依赖这个源。

```powershell
Copy-Item .env.example .env      # 填 Qdrant 密钥和本地模型路径
uv sync --locked

# 取回语料并校验双 Hash
uv run python scripts/fetch_corpus.py
uv run python scripts/verify_corpus_hashes.py

docker compose up -d qdrant
uv run python scripts/check_environment.py --models
# 建索引资产。两个入口**不读运行时注册表**，产物名取自已提交的目标配置，
# 所以新克隆（还没有注册表）也能直接跑。
uv run python scripts/build_dense_index.py --strategy blocks
uv run python scripts/build_bm25_index.py --strategy blocks

# 完整校验：确认 manifest、BM25、Qdrant 三份资产对得上。
uv run python scripts/manage_indexes.py validate configs/indexes/osg-3.6.5-blocks-dualhash.json

# 初始化运行时注册表（记录"当前 active 用哪个索引"）。它不进 Git，属于本机部署状态；
# 用 init 就地生成，已存在时会拒绝覆盖。必须在索引建好并通过校验之后。
uv run python scripts/manage_indexes.py init configs/indexes/osg-3.6.5-blocks-dualhash.json `
    --reason "本机首次初始化"

# 初始化访问策略。它同样不进 Git；少了这一步，检索、列表、下载**全部拒绝**
# （默认拒绝是设计行为，不是故障）。
uv run python scripts/manage_access.py --sync

uv run python scripts/search_docs.py "Viewer 的 run() 做什么？" --top-k 2
```

顺序不能颠倒：**语料 → 建索引 → 完整校验 → 注册表 → 访问策略 → 查询**。
建索引需要索引资产、注册表初始化需要索引已通过校验，把注册表放在建索引之前会形成
"建库要先有注册表、初始化注册表要先有索引"的循环。

启动 MCP 并调用：

```powershell
docker compose up -d mcp
uv run python scripts/test_mcp_client.py "Viewer 的 run() 做什么？"
```

TLS 与 Bearer 安全覆盖（自签名证书仅用于开发）：

```powershell
uv run python scripts/generate_dev_certificate.py --output-dir artifacts/tls `
  --hostname localhost --ip 127.0.0.1
docker compose -f compose.yaml -f compose.secure.yaml up -d
```

安全边界见 [SECURITY.md](SECURITY.md)。

## 多语料检索

五个新语料各有一套隔离的候选索引，通过显式 `library + version` 路由查询：

```powershell
uv run python scripts/search_routed.py --catalog
uv run python scripts/search_routed.py "projsync 支持哪些命令行参数？" --library PROJ
uv run python scripts/search_routed.py "WMS 和 osgEarth 的版本约定" --max-libraries 3
```

- 指定 `library` 时必须唯一匹配，版本不符会列出可用版本；省略 `library` 才跨库扇出，
  并受 `max_libraries` 限制，被跳过的库会在响应里显式列出。
- 路由在构造时**逐库校验**（manifest 指纹、BM25 行数、Qdrant 点数、维度、双 Hash 元数据），
  任一候选不合格就整体不建立。

### 通过 MCP 检索多语料

`search_docs` 支持指定语料；**不带参数时行为不变**，仍走 OSG active 单库：

```jsonc
{"query": "gdal_translate 支持哪些重采样方法", "library": "gdal"}   // 或 "target_id"
{"query": "Viewer 的 run() 做什么"}                                  // 仍是 OSG
```

- 同一个库有多个版本时只给 `library` 会报错并列出可选版本，再用 `version` 或
  `target_id` 指定；
- **按权限过滤发生在解析目标之前**：无权读取的语料不会出现在候选里，也不会被实际查询，
  且"无权"与"不存在"对外给出**完全相同**的报错——否则报错差异本身就说明了语料存在；
- 证据带自己的 `corpus_id` / `index_target_id` / `download_path`，不会被重标成别的语料；
- 未配置多语料候选清单时，指定 `library` 会明确报"未配置"，不会悄悄去查 OSG 索引。

先用 `list_corpora` 看有哪些库、哪些 `index_state=usable`。

## 实验与评测

历史模型对比、冻结集结果、切片和中文检索分析集中在[检索实验记录](docs/retrieval-experiments.md)。最新验证与限制见[当前状态](code.md)和[开发记录索引](docs/development-log.md)；历史数字不能当作当前部署验收。

## 添加自己的文档

```powershell
uv run python scripts/add_corpus.py --library mylib --version 1.0 --source ./我的文档 `
    --license MIT --source-url https://example.org/docs --visibility internal
```

一条命令完成：扫目录 → 认格式 → 算双 Hash → 生成 manifest → 归档原文 → 检查切片 →
建候选索引 → 打印切换命令（**不自动切换**）。

- `--visibility internal` 写入 `corpora-private/`（Git 忽略），`public` 写入 `corpora/`。
- 压缩包等容器格式只归档、不参与检索，原因如实写进 manifest。
- 标为可检索却切不出任何内容的文件会让整次入库**失败并指出文件名**，不会静默丢弃。

**扫描件**：逐页判定三态——有文字层 / 没有文字层但有整页图像（送 OCR）/ 两者都没有
（空白页或纯图形页，送 OCR 也没有内容可取）。只有第二类才触发确认，提示语只陈述
检测到的事实（页数、依据），不替用户下"这是扫描件"的结论。

确认前先探测本机 MinerU：服务不在则明确告知，并把含扫描页的文件转为只归档。
非交互环境用 `--ocr always` / `--ocr never` / `--yes`。

**当前行为**（与设计意图不同，按实际行为写明）：

- 同意 OCR 时，同类文件里同时有文字层页和扫描页的，**文字层页保留文本层、只有扫描页
  走 OCR**，不会整份重跑；文件级解析器记为 `mixed-text-ocr-page-v1`；
- **拒绝 OCR 时整份文件只归档、不参与检索**，包括其中本来完好的文字层页。
  `decide_ocr()` 会把这个文件整个排除，所以"保留文字层页继续索引"目前**没有实现**；
  `PdfPlan.text_layer_only` 与 `skipped_scan_pages` 那套分支在拒绝路径上不可达。

切出来的 chunk 带 `parser` / `ocr_derived`，证据会说明正文是不是识别来的。

```powershell
# 只想先看看会怎么处理，不写盘
uv run python scripts/add_corpus.py --library mylib --version 1.0 --source ./docs --dry-run
```

## 原文取件与访问控制

检索返回的每条证据都带 `download_path`；MCP 同端口提供原文件下载，**内网用户能直接查看
原文档本体**（PDF/HTML 浏览器内联打开），而不是只看抽出来的文本：

```
GET /files/{corpus_id}/{语料内登记路径}    # 复用同一个 Bearer 鉴权
```

路径是 **manifest 里登记的文件路径**（也就是 chunk 的 `source_path` 去掉
`raw_data/<subdir>/` 前缀之后的部分），不是磁盘上的 `raw_data/...` 相对路径。
两者容易混：OSG 的登记路径形如 `include/osg/Node`，PROJ 的形如 `man/man1/projsync.1`。
证据里直接给了拼好的 `download_path`，照着用即可。

访问策略默认**拒绝**：没有策略文件的语料，检索、列表、下载全部不放行。

```powershell
uv run python scripts/manage_access.py --sync              # 为已有语料生成初始策略
uv run python scripts/manage_access.py --list
uv run python scripts/manage_access.py --grant mylib-1.0 --client agent-a
```

- 公开语料策略为 `["*"]`（显式声明公开，未配置 Bearer 的部署也可读）；
- 内部语料**初始拒绝所有人**，必须显式 `--grant` 才能被读取；
- 被拒绝时返回 `403`，不伪装成 404。

MCP 另有 `list_corpora` 工具返回知识结构（库、版本、许可、文件数、索引状态），
**按调用方权限过滤**——没有权限的语料不会出现在列表里。

索引状态分三档，`has_index_config` 只表示"有配置提到它"：

| `index_state` | 含义 |
|---|---|
| `no_index_config` | 还没有索引目标引用这份 manifest |
| `usable` | 预检通过：manifest 字节未变、BM25 可读且行数相符、collection 在且点数相符 |
| `unusable` | 配置在但索引不可用（原因码见 `index_failures`） |

> **`usable` 是预检结论，不是验收结论**：它只查"能不能服务查询"这四件事，
> 不查 chunk 内容与位置。完整准入以
> `scripts/verify_candidate_indexes.py`（内部走 `admit_index`）为准。

> **边界**：本项目只**执行**访问策略，不**产生**策略。系统能识别的是"哪个客户端"
> （Bearer 密钥映射的 client_id），不是"哪个人"。内网部署应把 `configs/access/` 换成
> 从内网 IAM 读取的实现，代码接口（`AccessPolicy.allows`）不变。

## 索引维护

不在 active 索引上原地修改：

```text
新 manifest → 独立 Qdrant collection 与 BM25 → 逐库/三资产验证
→ 原子切换 active 注册表 → 重启 MCP → 冒烟 → 必要时回退
```

```powershell
uv run python -m scripts.build_multicorpus_candidates --generation 002 --strategy blocks-split
# 冻结前的只读复验：一次审计 configs/indexes 下的全部目标（含 active）。
# Qdrant 不可用时会明确报"没连上"并退出码 1，不会伪装成索引不合格或通过。
uv run python scripts/verify_candidate_indexes.py --all-targets
uv run python scripts/manage_indexes.py status
uv run python scripts/manage_indexes.py rollback --reason "..."
uv run python scripts/demo_content_update.py
uv run python scripts/demo_modify_delete.py
```

## 安全边界

- `.env`、本地模型、TLS 私钥、审计日志、生成的索引和原始语料都不提交 Git；
- **运行时注册表 `data/indexes/index-registry.json` 也不提交**。它记录当前 active 目标
  和切换历史；切到非公开目标后，active/history 里会有内部 manifest 路径与 collection 名，
  回退后 `events` 仍保留内部 target_id。用 `manage_indexes.py init` 就地生成；
- 审计默认只保存 Query / Session / 客户端 ID 的 SHA256；
- 当前 Bearer 是“一把密钥一个客户端 ID”，不是多用户 IAM，也没有吊销中心；
- 自签名 TLS 不替代企业 CA 或认证反向代理；
- 数据库过滤不能代替调用方鉴权。

**镜像里不带访问策略，也不带非公开语料**（`.dockerignore` 排除 `configs/access/`、
`corpora-private/`、`configs/indexes-private/`）。因此：

- `configs/access/` 必须在运行时挂载；不挂载时策略快照为空，判定是"默认拒绝所有人"
  ——容器能启动，但不服务任何语料，这是设计上的 fail closed，不是故障；
- **非公开语料不在镜像支持范围内**。要服务它们，需要同时挂载 `corpora-private/` 和
  `configs/indexes-private/`（镜像不烘焙这两处，也不为它们提供默认路径）。

## 许可证

项目自身代码以 **MIT** 许可（[LICENSE](LICENSE)）。

第三方语料**独立授权**，不因进入本仓库而改变：OpenSceneGraph（OSGPL）、osgEarth
（LGPL-3.0 含例外）、GDAL 与 PROJ（MIT 系）、OGC 规范（OGC Document Notice）。
本仓库不重新分发这些原始文件，只提交记录来源与哈希的 manifest；细则见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 文件管理入口

独立管理HTTP/CLI提供文件列表、上传判重、替换、删除、任务进度和失败重试。MCP通过同一个管理服务提供授权后的发现与检索；替换/删除后的新请求读取当前状态。受管删除不清理原始legacy索引、调用者源文件或外部备份。

- [文件管理指南](docs/file-management.md)：启动、凭据、HTTP/CLI、删除范围和MCP ACL配置。
- [管理界面说明](docs/admin-frontend.md)：浏览器界面、已有资料查询、逻辑分组、版本过期和调用日志。
- [项目目录与重构状态](docs/project-layout.md)：入口职责、四批验收进度及运行资产边界。
- [当前状态](code.md)：已验证范围、限制和下一步；示例命令不能代替验收证据。

## 文档导航

- [项目目标](PROJECT.md) · [当前状态与下一步](code.md)
- 文档职责：目标见 PROJECT.md，当前状态见 code.md，操作见以下专题，过程见开发记录索引。
- [环境](docs/environment.md) · [Docker部署](docs/docker-deployment.md)
- [MVP验收](docs/acceptance.md) · [Claude Code验收](docs/claude-code-acceptance.md)
- [文件管理](docs/file-management.md) · [管理界面](docs/admin-frontend.md)
- [项目目录与重构状态](docs/project-layout.md)
- [历史检索实验](docs/retrieval-experiments.md) · [开发记录索引](docs/development-log.md)
