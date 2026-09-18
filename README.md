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
  → Qdrant dense + SQLite FTS5 BM25
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

要求：Python 3.12、uv、Docker、NVIDIA GPU，以及本地 Qwen3-Embedding-0.6B 与
Qwen3-Reranker-0.6B 模型目录。

```powershell
Copy-Item .env.example .env      # 填 Qdrant 密钥和本地模型路径
uv sync --locked

# 取回语料并校验双 Hash
uv run python scripts/fetch_corpus.py
uv run python scripts/verify_corpus_hashes.py

docker compose up -d qdrant
uv run python scripts/check_environment.py --models
uv run python scripts/build_dense_index.py --strategy blocks
uv run python scripts/build_bm25_index.py --strategy blocks
uv run python scripts/manage_indexes.py validate configs/indexes/osg-3.6.5-blocks-dualhash.json
uv run python scripts/search_docs.py "Viewer 的 run() 做什么？" --top-k 2
```

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
- **尚未接入 MCP**：`search_docs` 目前仍只服务 OSG active 索引。

## 评测结果

全部为本机小样本结果，用于开发回归，**不构成生产结论或第三方盲测**。

冻结 OSG 8 题（全证据 @5）：baseline 5/8，加 0.6B 重排 7/8。

五库 validation 50 题（`candidate_k=20`、RRF `k=60`）：

| 检索器 | Recall@5 | 全证据@5 | MRR@5 |
|---|---:|---:|---:|
| dense | 0.850 | 0.82 | 0.687 |
| BM25 | 0.813 | 0.78 | 0.657 |
| RRF | 0.870 | 0.84 | 0.737 |
| + 0.6B 重排 | **0.970** | **0.96** | **0.917** |

重排单题延时 0.574 ± 0.125 秒（本机 GPU）。复现：

```powershell
uv run python evaluation/run_multicorpus_eval.py
```

已知失败：`rag-validation-050`（PROJ，单条证据跨 3 个 chunk）重排后在 @5 掉出，
@20 才覆盖；`rag-validation-021` 的证据分布在两个语料库，单库路由结构性无法覆盖。
逐题结果见
[`artifacts/corpus-acceptance-002/validation-retrieval-eval.json`](artifacts/corpus-acceptance-002/validation-retrieval-eval.json)。

## 切片

三种策略，`max_chars=1600`：

| 策略 | 说明 | 五库 chunk 数 | 超长 chunk |
|---|---|---:|---:|
| `blocks` | 按空行成块后合并；超长块整块成片 | 1496 | 162（最长 8563） |
| `blocks-split` | 同上，但超长块按行、必要时按字符切到硬上限 | 1691 | **0（最长 1600）** |

`blocks-split` 的片段不重叠且连续，保留原始行号/物理页码，单行被硬切时多个片段共享同一
行号但 chunk ID 唯一。两种策略下 validation 的 64 个证据区间都由 chunk 联合完整覆盖。

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

**扫描件**：检测到 PDF 页没有文本层时，先确认本机有没有 MinerU 服务，在线才问一次
「是否启用 OCR」；MinerU 不在则明确告知并把扫描件转为只归档。非交互环境用
`--ocr always` / `--ocr never` / `--yes`。

```powershell
# 只想先看看会怎么处理，不写盘
uv run python scripts/add_corpus.py --library mylib --version 1.0 --source ./docs --dry-run
```

## 原文取件与访问控制

检索返回的每条证据都带 `download_path`；MCP 同端口提供原文件下载，**内网用户能直接查看
原文档本体**（PDF/HTML 浏览器内联打开），而不是只看抽出来的文本：

```
GET /files/{corpus_id}/{raw_data相对路径}    # 复用同一个 Bearer 鉴权
```

访问策略默认**拒绝**：没有策略文件的语料，检索、列表、下载全部不放行。

```powershell
uv run python scripts/manage_access.py --sync              # 为已有语料生成初始策略
uv run python scripts/manage_access.py --list
uv run python scripts/manage_access.py --grant mylib-1.0 --client agent-a
```

- 公开语料策略为 `["*"]`（显式声明公开，未配置 Bearer 的部署也可读）；
- 内部语料**初始拒绝所有人**，必须显式 `--grant` 才能被读取；
- 被拒绝时返回 `403`，不伪装成 404。

MCP 另有 `list_corpora` 工具返回知识结构（库、版本、许可、文件数、是否已建索引），
**按调用方权限过滤**——没有权限的语料不会出现在列表里。

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
uv run python scripts/verify_candidate_indexes.py
uv run python scripts/manage_indexes.py status
uv run python scripts/manage_indexes.py rollback --reason "..."
uv run python scripts/demo_content_update.py
uv run python scripts/demo_modify_delete.py
```

## 安全边界

- `.env`、本地模型、TLS 私钥、审计日志、生成的索引和原始语料都不提交 Git；
- 审计默认只保存 Query / Session / 客户端 ID 的 SHA256；
- 当前 Bearer 是“一把密钥一个客户端 ID”，不是多用户 IAM，也没有吊销中心；
- 自签名 TLS 不替代企业 CA 或认证反向代理；
- 数据库过滤不能代替调用方鉴权。

## 目录结构

```text
src/wenjin/                  正式源代码
  content_hashing.py         双 Hash、规范化 profile 与语料内容指纹
  paths.py                   路径安全原语（防目录逃逸与符号链接）
  access.py                  语料访问策略（默认拒绝）
  serving.py                 原文取件定位（不含 HTTP）
  catalog.py                 语料目录与索引状态
  ingestion/                 多格式解析、切片、入库、MinerU OCR
  indexing/                  Qdrant、BM25 与候选构建
  retrieval/                 Query 处理、RRF、Reranker、Service、跨库 Router
  mcp_server.py              状态化 MCP：search_docs / list_corpora / /files 取件
  index_registry.py          active 索引校验、切换与回退
tests/                       自动化测试
evaluation/                  validation 对照、冻结集评测、RAGAS 独立环境
scripts/                     导入、建库、查询、索引管理与实验 CLI
corpora/<corpus-id>/         可提交的 manifest、来源与许可元数据
corpora-private/<corpus-id>/ 非公开语料（Git 忽略）
configs/access/              访问策略（Git 忽略，属于部署配置）
raw_data/<library>/<version> 语料原文（不提交 Git）
configs/indexes/             索引目标与候选路由清单
artifacts/                   实验输出与验收证据（大文件与第三方副本除外）
```

## 尚未完成

- 多语料路由接入 MCP（`search_docs` 目前仍只服务 OSG active）；
- 新语料的自动化获取脚本（`fetch_corpus.py` 只支持 OSG 风格 manifest）；
- 扫描件 OCR 的表格/版式质量提升（正文相似度 0.98，表格页仅 0.69~0.78）；
- 访问策略对接内网 IAM（当前为静态 ACL，且不提供身份认证）；
- 跨机器联调、企业 CA、多用户权限、并发压测；
- 离线镜像在目标内网的导入与验收；
- 父子片段、文档级路由、内网原文查看器；
- PDF 表格关系与复杂 HTML DOM 的独立质量验收；
- RAGAS 真实业务指标与 Faithfulness 评测。

## 许可证

项目自身代码以 **MIT** 许可（[LICENSE](LICENSE)）。

第三方语料**独立授权**，不因进入本仓库而改变：OpenSceneGraph（OSGPL）、osgEarth
（LGPL-3.0 含例外）、GDAL 与 PROJ（MIT 系）、OGC 规范（OGC Document Notice）。
本仓库不重新分发这些原始文件，只提交记录来源与哈希的 manifest；细则见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
