"""small-to-big：命中 child 之后换成它的父块再返回。

父子化索引里 child 负责命中率（片段小、向量更聚焦），父块负责给证据足够上下文。
替换必须发生在**融合之前**：同一父块会被多个 child 各自命中，如果在融合之后才去重，
同一段正文会占掉多个 top-K 名额，把别的证据挤出去——结果看起来只是"少了几条"，
不会显得更差，属于难以察觉的退化。

替换只改"这段正文来自哪里"（source_path/行区间/正文/内容Hash），不动检索分数与
路由归属：分数由检索方标注，父块行没有也不该有这些字段。
"""

from pathlib import Path

from wenjin.indexing.bm25 import load_parents

# 替换后必须由**父块**提供的字段。证据契约要求它们逐字来自原文的同一段行区间，
# 所以一个都不能从 child 那边留下。
PARENT_FIELDS = (
    "chunk_id",
    "source_path",
    "version",
    "start_line",
    "end_line",
    "strategy",
    "content_sha256",
    "source_raw_sha256",
    "source_canonical_sha256",
    "normalization",
    "page_number",
    "logical_document_id",
    "parser",
    "ocr_derived",
    "text",
)
# 保留候选自带的字段：分数与归属由检索方标注，不能被父块行覆盖（同 router 里把身份
# 字段放在 payload 之后的理由）。`rerank_score`/`pre_rerank_rank` 只在"重排之后才换成
# 父块"的路径上存在，但那时它们正是这条证据的名次依据，丢了就等于证据说不清自己为什么
# 排在前面。
CANDIDATE_FIELDS = (
    "score",
    "raw_bm25",
    "matched_terms",
    "library",
    "target_id",
    "index_target_id",
    "rerank_score",
    "pre_rerank_rank",
)


def parent_identity(item: dict):
    """融合身份：同一父块的多个 child 必须合成一条候选。

    没有 `parent_id` 的候选（fixed/blocks/blocks-split 索引）行为与按 chunk_id 融合
    完全一致，所以这条身份可以直接用在所有索引上。
    """
    return (item.get("target_id"), item.get("parent_id") or item["chunk_id"])


def resolve_parents(candidates: list[dict], *, bm25_path: Path) -> list[dict]:
    """把带 `parent_id` 的候选换成父块；其余候选原样返回。

    没有候选带 `parent_id` 时不读索引文件，因此对非父子化索引零开销。
    父块取不到是**资产损坏**（准入审计要求每个 child 的父块都在且区间包含它），
    这里报错而不是悄悄退回 child：退回 child 会让证据悄悄少掉上下文，看上去还是正常的。
    """
    wanted = {item["parent_id"] for item in candidates if item.get("parent_id")}
    if not wanted:
        return candidates
    parents = load_parents(bm25_path, wanted)
    resolved: list[dict] = []
    for item in candidates:
        parent_id = item.get("parent_id")
        if not parent_id:
            resolved.append(item)
            continue
        parent = parents.get(parent_id)
        if parent is None:
            raise ValueError(
                f"候选引用了取不到的父块 {parent_id}：索引缺少父块行或未经准入审计"
            )
        resolved.append(
            {
                **{field: parent[field] for field in PARENT_FIELDS},
                **{field: item[field] for field in CANDIDATE_FIELDS if field in item},
                "parent_id": parent_id,
                # 命中的是哪个 child 属于诊断信息：证据引用的是父块，但排查命中原因时
                # 需要知道是哪一小段把它召回的。
                "child_chunk_id": item["chunk_id"],
            }
        )
    return resolved
