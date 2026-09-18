"""Reciprocal Rank Fusion：只使用各检索器的排名融合候选。"""


def rrf_fuse(result_sets: dict[str, list[dict]], *, k: int = 60) -> list[dict]:
    """按 chunk_id 合并多个排名列表；rank 从 1 开始。"""
    if k < 0:
        raise ValueError("RRF k must be non-negative")
    merged: dict[str, dict] = {}
    for retriever, results in result_sets.items():
        for rank, result in enumerate(results, start=1):
            chunk_id = result["chunk_id"]
            if chunk_id not in merged:
                # 保存一份候选正文和来源；各路原始分数单独记录，不拿来相加。
                merged[chunk_id] = {
                    key: value for key, value in result.items()
                    if key not in {"score", "raw_bm25", "matched_terms"}
                }
                merged[chunk_id]["rrf_score"] = 0.0
                merged[chunk_id]["ranks"] = {}
                merged[chunk_id]["original_scores"] = {}
            merged[chunk_id]["rrf_score"] += 1.0 / (k + rank)
            merged[chunk_id]["ranks"][retriever] = rank
            merged[chunk_id]["original_scores"][retriever] = result.get("score")
    return sorted(
        merged.values(),
        key=lambda item: (-item["rrf_score"], min(item["ranks"].values()), item["chunk_id"]),
    )


def interleave_unique(result_sets: dict[str, list[dict]]) -> list[dict]:
    """按 query 轮询取候选，让每个子问题都有机会贡献证据。"""
    if not result_sets:
        return []
    merged = []
    positions: dict[str, int] = {}
    longest = max((len(results) for results in result_sets.values()), default=0)
    for rank in range(longest):
        for route, results in result_sets.items():
            if rank >= len(results):
                continue
            result = results[rank]
            chunk_id = result["chunk_id"]
            if chunk_id in positions:
                merged[positions[chunk_id]]["rewrite_routes"][route] = rank + 1
                continue
            copied = dict(result)
            copied["rewrite_routes"] = {route: rank + 1}
            positions[chunk_id] = len(merged)
            merged.append(copied)
    return merged
