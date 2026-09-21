"""查询侧不变性：给查询加上中文词，会不会改变英文语料上的 BM25 结果？

这是"已发布评测数字还算不算数"的直接依据，所以要单独测、单独报：

- **索引不重建**（现役索引）：query_terms 多出来的中文词在英文语料里匹配不到东西。
  要验证的是 FTS5 的 bm25() 评分**不会**被匹配不到的 OR 词项影响——如果会，说明得分
  和排序都会漂，已发布的数字立刻作废。
- **索引重建后**（候选索引）：英文 chunk 的 retrieval_text 逐字节未变，同样要比。

对每一条查询分别跑"改造前的词表（只取 ASCII 标识符）"与"改造后的词表（ASCII + 中文）"，
逐条比对返回行与得分。唯一预期会变的是 `matched_terms` 这个**记录字段**——
`fusion.py` 已显式把它排除在 RRF 之外，它不是评分输入。

不使用冻结测试集；下面的查询是本脚本自造的。

用法：`./.venv/Scripts/python.exe artifacts/chinese-bm25/verify_query_invariance.py`
"""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

ACTIVE = ROOT / "data/indexes/osg_3_6_5_bm25_blocks_dualhash.sqlite"
CANDIDATE = ROOT / "artifacts/chinese-bm25/candidate-bm25-osg-3.6.5-blocks.sqlite"
OUT = ROOT / "artifacts/chinese-bm25/query-invariance.json"

# 自造查询，不取自冻结集。前 8 条是"中文问题 + 内嵌英文标识符"（冻结集的形态），
# 最后 2 条是纯英文对照与纯中文（本次修复真正新增覆盖的场景）。
QUERIES = [
    "如何使用 addChild 添加子节点",
    "GeoTransform 的仿射变换参数是什么",
    "osg::Geode 和 Drawable 有什么区别",
    "readRefImageFile 如何加载图片",
    "setTextureAttributeAndModes 怎么绑定纹理单元",
    "ref_ptr 是什么，如何管理节点生命周期",
    "Group 的 setReferenceFrame 设置的是什么",
    "addDrawable 和 addChild 有什么区别",
    "Node Visitor",            # 纯英文对照
    "节点如何遍历",             # 纯中文：改造前完全不走 BM25
]


def ascii_only_terms(query: str) -> list[str]:
    """改造前 query_terms 的规则：只取 ASCII 标识符，保持出现顺序去重。"""
    from wenjin.indexing.bm25 import ASCII_TERM

    terms, seen = [], set()
    for term in ASCII_TERM.findall(query):
        lowered = term.lower()
        if lowered not in seen:
            seen.add(lowered)
            terms.append(term)
    return terms


def score_signature(hits: list[dict]) -> list[tuple]:
    return [(hit["chunk_id"], hit["raw_bm25"]) for hit in hits]


def rows_without_matched_terms(hits: list[dict]) -> list[dict]:
    return [{k: v for k, v in hit.items() if k != "matched_terms"} for hit in hits]


def run(index: Path) -> dict:
    from wenjin.indexing.bm25 import bm25_query, query_terms

    results = {}
    for query in QUERIES:
        old_terms = ascii_only_terms(query)
        new_terms = query_terms(query)

        # 改造前：纯中文 query 提取不到词，分路被整条跳过（service.py 的 `if terms`）。
        if old_terms:
            old_rows = bm25_query(index, query, top_k=20, terms_override=old_terms)
            old_error = None
        else:
            old_rows, old_error = [], "ValueError: 没有从 query 中提取到英文或代码标识符"

        new_rows = bm25_query(index, query, top_k=20)
        assert new_rows == bm25_query(index, query, top_k=20, terms_override=new_terms)

        results[query] = {
            "old_terms": old_terms,
            "new_terms": new_terms,
            "added_by_cjk": [t for t in new_terms if t not in old_terms],
            "old_skipped_bm25": old_error,
            "old_hits": len(old_rows),
            "new_hits": len(new_rows),
            "rows_identical_ignoring_matched_terms": (
                rows_without_matched_terms(old_rows) == rows_without_matched_terms(new_rows)
            ),
            "scores_bit_identical": score_signature(old_rows) == score_signature(new_rows),
            "matched_terms_is_the_only_diff": (
                old_rows != new_rows
                and rows_without_matched_terms(old_rows) == rows_without_matched_terms(new_rows)
            ),
        }
    return results


def main() -> None:
    report = {
        "purpose": "加中文词是否改变英文语料上的 BM25 返回行与得分",
        "queries_are_self_authored": True,
        "frozen_set_used": False,
        "indexes": {},
    }
    for label, path in (("active_not_rebuilt", ACTIVE), ("candidate_rebuilt", CANDIDATE)):
        report["indexes"][label] = run(path)

    for label, results in report["indexes"].items():
        report.setdefault("summary", {})[label] = {
            "queries": len(results),
            "rows_identical_ignoring_matched_terms": all(
                item["rows_identical_ignoring_matched_terms"] for item in results.values()
            ),
            "scores_bit_identical": all(
                item["scores_bit_identical"] for item in results.values()
            ),
            "queries_where_bm25_was_skipped_before": [
                query for query, item in results.items() if item["old_skipped_bm25"]
            ],
            "queries_whose_terms_changed": [
                query for query, item in results.items() if item["added_by_cjk"]
            ],
        }

    with closing(sqlite3.connect(ACTIVE)) as database:
        report["active_chunk_count"] = database.execute(
            "SELECT COUNT(*) FROM chunks").fetchone()[0]
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print("written", OUT.name)


if __name__ == "__main__":
    main()
