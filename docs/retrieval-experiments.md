# 检索实验与历史结果

以下内容于2026-10-07从原README迁出，保留原实验口径和边界，不是本轮重测。日常使用见[README](../README.md)，最新状态见[code.md](../code.md)，后续迭代见[开发记录索引](development-log.md)。

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
[`artifacts/frozen-test-001/ragas-answer-relevancy.json`](../artifacts/frozen-test-001/ragas-answer-relevancy.json)。

逐题结果与判分可靠性记录见
[`artifacts/frozen-test-001/ragas-retrieval-eval.json`](../artifacts/frozen-test-001/ragas-retrieval-eval.json)。

**运行记录与已知修订**：跑之前发现 21 道 3D Tiles 题的 `library` 写成显示名
`"OGC 3D Tiles"`，经归一化得到 `ogc-3d-tiles`，而语料 id 是 `ogc-3dtiles`，**无法路由**
（validation 里没有 3D Tiles 题，这条路径从未被走过）。只改了 `library` 一列并更新
`dataset_revision`，题目/查询/证据/标签未动，**改在见到任何结果之前**。该题集现已
`evaluated=true`，不再作为未用过的考卷。运行记录见
[`artifacts/frozen-test-001/run-record.json`](../artifacts/frozen-test-001/run-record.json)，
逐题结果见
[`artifacts/frozen-test-001/test-retrieval-eval.json`](../artifacts/frozen-test-001/test-retrieval-eval.json)。

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
[`artifacts/corpus-acceptance-002/validation-retrieval-eval.json`](../artifacts/corpus-acceptance-002/validation-retrieval-eval.json)。

## 切片

下面的数字是 **2026-09-18 的旧 002 候选统计**，不是当前源码重建结果：

| 旧候选策略 | 当时的说明 | 五库 chunk 数 | 当时统计的超长 chunk |
|---|---|---:|---:|
| `blocks` | 按空行成块后合并；超长块整块成片 | 1496 | 162（最长 8563） |
| `blocks-split` | 按行切分，旧实现还会把过长单行按字符硬切 | 1691 | **0（最长 1600）** |

旧 `blocks-split` 统计掩盖了证据定位问题：审计在 3D Tiles 发现 22 行被硬切，涉及 58 个
旧片段，最长行 4964 字符；这些片段虽有唯一 chunk ID，却共享整行行号，正文并不等于声明的
原文行区间。当前切片源码保留完整物理行，不再按字符截断。旧 002 不会因源码规则更新而自动改变。

2026-09-29，新建隔离候选 `ogc-3dtiles-1.1-blocks-split-candidate-003`：989 个 chunk，
其中 HTML 306、PDF 683。生产三资产准入为 PASS；其默认原文审计受 8 MiB 和 PDF 页级范围限制，
报告 0/989 可逐字核验。另一次针对该候选的只读专项以双 Hash 验证后的字节重放生产 HTML 行解析器
及 PyMuPDF 页解析器，306/306 个 HTML chunk 和 683/683 个 PDF 页内切片均与声明位置的解析正文逐字
一致，所有 PDF 页码也在物理页范围内。这项专项结果不改变默认审计的覆盖边界。后续该候选已进入
本机默认多库候选路由003，并通过真实 Streamable HTTP MCP 路由冒烟；默认审计仍报告0/989，因为它的
8 MiB/PDF页级界限未改变，专项 source replay 才是989/989逐字与位置复核的证据。另有固定24题
3D Tiles长行补充评估：固定候选池上的24题mixed-frame新推理与缓存baseline相比，selected locator覆盖
13/24→16/24；独立语义审查确认20/20规范题有支持且baseline原有支持未回退。这不是全新retrieval，
仍属专门压力集；经合同及语义独立审核，source-context helper 已限定接入登记的3D Tiles 1.1 target，不能据此宣称通用validation或生产质量保证。阶段4本机Windows MCP/registry E2E通过，远端与生产仍未验收。详细范围见
[`code.md`](../code.md)及`artifacts/phase1-source-replay-validation/`。

旧 validation 对照共 64 个证据区间，**按判定依据拆开看**（两种旧策略当时都是同样结果）：

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

