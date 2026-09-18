# OSG 冻结测试集 001

`osg-3_6_5-frozen-001.jsonl` 包含8道助理编写并逐条核对源码的可回答题。
它在既有开发集规则、query改写提示词和0.6B重排实现完成后建立；每题保留原文件与行号，
不依赖chunk ID。文件SHA256为
`caa84676cfc343629e52b46249524c5e05a27c8cdd3c2794bc55fe4b3af09562`；冻结后不得为了提高分数修改题目、证据
或检索规则。若标注确有事实错误，记录更正原因并另建 `002` 版本，不能静默覆盖。

这份数据是**开发过程外留的助理编写题**，不是用户真实提问，也不是完全独立的第三方盲测。
第005、006题虽与开发集使用同一文件，但测试不同API；因此仍可能有领域家族泄漏。
后续要补真实用户题、跨版本和不可回答题。不可回答题需要拒答/阈值评测，不能直接套用
“证据覆盖率”计算。

0.6B与4B同候选报告分别为 `artifacts/runs/osg-frozen-001-0_6b-b2.json` 和
`artifacts/runs/osg-frozen-001-4b-b2.json`。两轮候选ID指纹相同：
`be9537f89b37ce09f07e5f2ca3e5677f71552a9b865c8aae6e8d222686c829b5`。


# 跨语料题集001（2026-09-17导入）

`rag-validation-001.jsonl` 50题；`rag-test-001.jsonl` 40题。题集修订前从未用于检索评估，
按用户确认保留ID并重新封存；这是新跨语料集的首次评估前修订，不改变上面OSG旧冻结集规则。
校验和见同名 `.sha256`；完整旧新Hash、20题修订和来源核验见
[验收报告](../../artifacts/corpus-acceptance-001/REPORT.md)。

精确可检索范围：`rag-multicorpus-scope-001.json`。PROJ包含7份man手册。
validation使用osgEarth/PROJ/WMS，test使用GDAL/3D Tiles；PDF与HTML按同一标准逻辑身份识别。
所有evidence.path相对项目根，指向`raw_data/`；行号从1开始，PDF页码为物理页码，从1开始。
证据自身raw/canonical Hash与归档Hash分开；归档来源与成员身份详见对应source-manifest。

验证：`uv run python -m scripts.verify_imported_datasets --output artifacts/corpus-acceptance-001/imported-datasets.json --self-test`。
`required_evidence_atoms`补充自动日期/版本/标识符检查；命中不等于语义必然正确。
review_status为agent_source_checked，未宣称人工审查。test仅做数据质量审查，尚无检索分数。
后续调参只使用validation；修复假负样本后validation没有完全不可回答题，test只有1题。
格式适配完成后才能建新索引，不要直接把原始HTML/base64、PDF或man交给文本切片器。
