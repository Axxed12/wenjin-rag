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

## 评测结果

全部为本机小样本结果，用于开发回归，**不构成生产结论或第三方盲测**。
题集由本项目自行编写（含 Agent 审查），不是第三方盲测。

**两套数字要分开读**：`validation`（50 题）用来挑选方案，方案就是按它的表现定的，
因此偏乐观；`test`（40 题）在此之前**封存未用**，只在方案定死后跑过一次。
**对外的预期性能应引用 test 的数字。**

| 题集 | 用途 | 重排 @5 全证据 | 重排 @5 MRR |
|---|---|---:|---:|
| validation 50 题 | 挑方案（偏乐观） | 0.96 | 0.917 |
| **test 40 题** | **封存后只跑一次** | **0.750** | **0.753** |

冻结 OSG 8 题（全证据 @5）：baseline 5/8，加 0.6B 重排 7/8。

### 冻结 test 40 题：首次也是唯一一次运行

题集 `evaluation/datasets/rag-test-001.jsonl`，40 题覆盖 GDAL（19）与 OGC 3D Tiles（21），
51 条证据（41 条行级 + 10 条 PDF 页级）。配置与 validation 相同
（`candidate_k=20`、RRF `k=60`、002 候选、Qwen3-Reranker-0.6B）。

| 检索器 | @5 span_recall | @5 全证据 | @5 任一命中 | @5 MRR | @20 全证据 |
|---|---:|---:|---:|---:|---:|
| dense | 0.675 | 0.625 | 0.725 | 0.593 | 0.850 |
| BM25 | 0.575 | 0.525 | 0.625 | 0.430 | 0.700 |
| RRF | 0.650 | 0.625 | 0.675 | 0.562 | 0.825 |
| + 0.6B 重排 | **0.812** | **0.750** | **0.875** | **0.753** | **0.875** |

重排单题延时 0.758 ± 0.099 秒（本机 GPU）。

分库看，**两个库的问题不一样**（重排 @5）：

| 库 | 题数 | 全证据 | MRR | 瓶颈 |
|---|---:|---:|---:|---|
| GDAL | 19 | 0.842 | **0.974** | 召回：证据进了候选池几乎都能排到最前 |
| OGC 3D Tiles | 21 | 0.667 | 0.553 | 召回与排序都弱：该库仅 2 个文件却切出 1025 个 chunk，候选池大 |

**发现**：

- 真实水平约为 test 的 **0.750**，低于 validation 的 0.96。两套题覆盖的库不同
  （validation 为 osgEarth/WMS/PROJ，test 为 GDAL/3D Tiles），因此这 0.21 的差
  既包含"按 validation 调过参"，也包含"这两个库本身更难"，**现有数据分不开这两者**。
- GDAL 的瓶颈是召回而非排序（MRR 0.974 但全证据 0.842）；提升应从召回入手，不是重排。
- 5 道题连 @20 都取不到（`013/014/015/037/038`），属未召回而非排错。

**读数要注意的口径**：

- 页级证据（10 条）只判"该页有 chunk"，不校验原文是否落在文本里；
- 指标把登记的每条证据都视为必需，包括 `expected_status=insufficient` 的 `rag-test-040`
  ——那题"未被覆盖"本身可能就是正确行为；
- **空行不算证据**。切片按空行分块，连续空行里靠后的那些不属于任何 chunk（这是切片
  正确行为）。早期实现要求证据区间**每一行**都被覆盖，把"内容全在、中间隔了个空行"
  判成未覆盖——例如证据 199-209、两段 chunk 分别是 194-204 和 206-222，缺的第 205 行
  是 `</div>`（解析后为空）。修正后重排 @5 全证据由 0.675 升到 0.750，**检索本身未变**
  （40/40 题 top-5 chunk ID 与修正前逐字节一致）。判定实现在 `src/wenjin/coverage.py`
  与 `evaluation/run_retrieval_eval.covers_span`。

### RAGAS 复核（语义口径，与上面的词法口径互为对照）

判分模型 DeepSeek `deepseek-flash`，`reference` 用题集的 `answer_rubric`。

**两个口径在问不同的问题**，数值不可互替：

- 上面的 span_recall / 全证据是**词法·位置**判定：**登记的那几段证据**取回来了吗？
- RAGAS 是**语义·答案**判定：取回来的东西**够不够推导出参考答案**？

RAGAS 四大核心指标**四个都跑了**，但只有两个纳入结论：

| 指标 | 均值 | 最低 | 可用性 |
|---|---:|---:|---|
| `context_recall` | **0.9517** | 0.33 | 可用 |
| `context_precision` | **0.9642** | 0.75 | 可用 |
| `faithfulness` | 0.9228 | 0.40 | **不可用（信度失败），不纳入结论** |
| `answer_relevancy` | 0.8669 | 0.00 | **不可用（效度失败），不纳入结论** |

两个被弃用的失败方式**不同**，这个区分比数值本身重要：`faithfulness` 是**信度**问题
（同一份输入连判两次得 0.4 / 0.667，波动 0.27）；`answer_relevancy` 是**效度**问题
（噪声只有 0.025，但**标准答案反而输给被测答案**）。详见下文两小节。

RAGAS 明显高于词法口径，因为题集自己写着 `listed spans jointly support the rubric;
not exhaustive`——登记证据是**充分条件而非唯一解**，别处有同样内容也能答对。

**为什么说判分可用**（受控验证，不是只看分数）：它给 `expected_status=insufficient`
的 `rag-test-040` 打 0.0、给 `partially_supported` 的 039 打 0.75，与标注一致；
两轮重跑的逐题波动是 3/40 题、题均绝对差 0.0175。

#### 生成答案与 `faithfulness`：做了，但判分不可信

为跑 `Faithfulness` 加了一层生成：**讯飞 `xopqwen35397b`** 依据检索到的 top-5 作答，
`reference` 对照 `answer_rubric`。**这层只存在于评测里**——服务端仍然不生成答案
（MCP 返回 `answer_status: not_generated`），契约没变。40 题全部生成成功，平均 486 字。

但**判分结果不可信，因此不作为结论**。三条受控证据：

| 测试 | 期望 | 实测 |
|---|---|---|
| 把**检索到的上下文原文本身**当答案 | 1.0 | **0.857** |
| 同一份输入连续判两次 | 一致 | **0.4 / 0.667** |
| 判分链路是否通则（context 两项） | 正常 | 正常 |

同一批低分题的答案人读起来是对的（如 016 答出了 GDAL 3.11、`CPL_DEBUG`、默认不告警，
与参考一致），却只得 0.4。**所以这是指标配这个判分模型时的噪声，不是生成质量差。**

同理 `FactualCorrectness` 也试过并弃用：答案=参考得 1.0、答案正确但更长得 0.0、
把两者对调又变回 1.0——结果严重依赖方向。`AnswerCorrectness` 没跑：它同样需要
embedding，且内部含 factual 类判分，而那一类已被判定不可信，重复同一个坑不值得。

**四个指标都跑了，只报两个。**

#### `answer_relevancy`：不是噪声问题，是方向性错误

四大核心指标里唯一没跑过的那个已补上：**0.8669 ± 0.1769**（n=40，0 题失败）。
它**同样不纳入结论**，但理由与 `faithfulness` 完全不同：

| 受控对照 | 均值 | 说明 |
|---|---:|---|
| **参考 rubric 原文当答案** | **0.7949** | 这是人写的标准答案，**却低于被测答案** |
| 被测的生成答案 | 0.8669 | 40 题里 **31 题是标准答案更低** |
| 检索上下文原文当答案 | 0.7822 | |
| 问题原文当答案（链路上界） | 0.2043 | 原始余弦 0.9282，**上界本就到不了 1.0** |

另外三条支撑：

- **分数跟着答案长度走**：最短 13 题（均长 221 字）0.7909，最长 13 题（均长 790 字）0.9074；
- **跨语言污染**：中文题面、答案也是中文，但倒推出的问题有 **28% 变成英文**，
  跨语言余弦系统性偏低，与答案质量无关；
- **合规回答被判零分**：`rag-test-040` 是数据集要求 `expected_status=insufficient` 的题，
  答案合规地回"资料不足"，倒推问题与原问题几乎相同（原始余弦 0.973），
  却被判 noncommittal **直接乘 0** ——**服务按契约说"证据不足"反而被这个指标判零分。**

**一个指标连标准答案都认不出来，就不能拿它衡量答案。**

复跑入口 `evaluation/run_answer_relevancy_eval.py`。**没有重跑检索、没有重新生成答案**
——直接复用原先留存的 40 份答案，所以没有二次消费冻结集。逐题记录与可靠性验证见
[`artifacts/frozen-test-001/ragas-answer-relevancy.json`](artifacts/frozen-test-001/ragas-answer-relevancy.json)。

逐题结果与判分可靠性记录见
[`artifacts/frozen-test-001/ragas-retrieval-eval.json`](artifacts/frozen-test-001/ragas-retrieval-eval.json)。

**运行记录与已知修订**：跑之前发现 21 道 3D Tiles 题的 `library` 写成显示名
`"OGC 3D Tiles"`，经归一化得到 `ogc-3d-tiles`，而语料 id 是 `ogc-3dtiles`，**无法路由**
（validation 里没有 3D Tiles 题，这条路径从未被走过）。只改了 `library` 一列并更新
`dataset_revision`，题目/查询/证据/标签未动，**改在见到任何结果之前**。该题集现已
`evaluated=true`，不再作为未用过的考卷。运行记录见
[`artifacts/frozen-test-001/run-record.json`](artifacts/frozen-test-001/run-record.json)，
逐题结果见
[`artifacts/frozen-test-001/test-retrieval-eval.json`](artifacts/frozen-test-001/test-retrieval-eval.json)。

### validation 50 题（用于挑选方案）

`candidate_k=20`、RRF `k=60`：

| 检索器 | Recall@5 | 全证据@5 | MRR@5 |
|---|---:|---:|---:|
| dense | 0.850 | 0.82 | 0.687 |
| BM25 | 0.813 | 0.78 | 0.657 |
| RRF | 0.870 | 0.84 | 0.737 |
| + 0.6B 重排 | **0.970** | **0.96** | **0.917** |

> **"全证据@5" 是混合口径，两列不可互换读**：文本类证据要求命中的 chunk 覆盖到证据
> **行区间**；PDF 证据只登记物理页、没有行号或锚点，只能判"该页有 chunk"。
> 一张表里同时含这两种证据，"全证据"不等于每段证据的原文都被完整覆盖。

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
行号但 chunk ID 唯一。

validation 共 64 个证据区间，**按判定依据拆开看**（两种策略都是同样结果）：

| 依据 | 区间数 | 结果 |
|---|---:|---|
| 证据文本（按行位置合并，校验文本与范围一致） | 51 | 0 失败 |
| PDF 物理页（只能判"该页有 chunk"） | 13 | 不是证据原文覆盖 |

所以能说的是：**51 个文本证据区间完整进入 chunk**；13 个 PDF 证据只做到"该页有 chunk"，
原文是否落在切出来的文本里**没有验证**，不能并进同一个数字里说"64 个全部覆盖"。
两种依据的定义见 `src/wenjin/coverage.py`。

## 中文检索

FTS5 默认的 `unicode61` 分词器把**一整段连续中文当成一个词元**：`本接口用于查询用户信息`
只索引出这一个词元，于是查 `接口` 命中 0 条。更糟的是**一个英文标识符都不带的**纯中文
query 提取不到任何词，`bm25_query`/`fts_query` 直接抛错，`service.py`/`router.py` 里的
`if terms` 开关把 BM25 分路整条跳过。

**适用范围要说清**：带英文标识符的中文查询（"GeoTransform 怎么用"、"addChild 添加子节点"）
旧行为下本来就能提取到英文词，BM25 一直在出力，**不受本次改动影响**。修复解决的是纯中文
查询与纯中文文档——国内内网资料最常见的形态。

现在索引与查询两侧都用 `src/wenjin/indexing/zh.py` 做同一次 jieba 切分：

- 写入：`retrieval_text` 存 `segment_cjk(路径 + 正文)`，中文段变成空格分隔的词。
- 查询：`query_terms()` 在 ASCII 标识符之后追加中文词。ASCII 仍在前。
- **表结构不变**（仍是 `tokenize='unicode61'`，仍只有 `retrieval_text` 被索引），
  `raw_text` 存的仍是未切分原文，`bm25_query` 返回的 `text` 取自 `raw_text`——
  引用给用户的证据原文一个字节都没变。
- 无中文的文本 `segment_cjk` 逐字节原样返回，且不加载 jieba，纯英文语料不受影响。

### 索引与查询必须用同一版 jieba

**换 jieba 版本必须重建 BM25 索引。** 两边切分一旦不一致，索引里的词元和查询词元就
对不上，中文检索会**静默返回空结果**（不报错、不告警），只有向量一路在干活——和没做
这次改动一样难发现。`uv.lock` 锁死了版本，升级时请连同重建索引一起做。

jieba 是重依赖（import + 建前缀词典约 0.5 s），因此是**惰性加载**的：任何模块的顶层
import 都不会把它带进来（`import wenjin.mcp_server` 实测不加载 jieba）。服务在
`create_runtime()` 里调一次 `prewarm()`，避免首个检索请求吃掉这半秒，也避开线程池里
两个请求同时首次初始化。

已知运维风险，不粉饰：jieba 0.42.1 自 2020-01-20 起未再发版；import 时会经
`pkg_resources` 打一条弃用告警（`setuptools` 已计划移除该 API）；初始化日志走 stderr
（已用测试锁住，MCP 的 stdio stdout 不被污染）；临时目录不可写时词典每次启动重建
（不崩，只记一条 `Dump cache file failed.`）。

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
