"""Reciprocal Rank Fusion：只使用各检索器的排名融合候选。"""

from collections.abc import Callable


def rrf_fuse(
    result_sets: dict[str, list[dict]],
    *,
    k: int = 60,
    identity: Callable[[dict], object] | None = None,
) -> list[dict]:
    """合并多个排名列表；rank 从 1 开始，重复出现的候选按名次累加。

    `identity` 决定"两条候选是不是同一个"，默认用 `chunk_id`——这在**单个**索引内
    是唯一的。跨索引融合时必须传入包含目标身份的键，否则两个目标里同名的 chunk
    会被并成一条：正文取先出现的那份，分数却叠加了双方的排名贡献。

    键只在**同一份候选列表内部**需要唯一；不同检索器之间用同一个键表示"同一条候选"，
    正是靠它合并且分别记录各自的名次。
    """
    if k < 0:
        raise ValueError("RRF k must be non-negative")
    key_of = identity or (lambda result: result["chunk_id"])
    merged: dict[object, dict] = {}
    for retriever, results in result_sets.items():
        for rank, result in enumerate(results, start=1):
            key = key_of(result)
            if key not in merged:
                # 保存一份候选正文和来源；各路原始分数单独记录，不拿来相加。
                merged[key] = {
                    field: value for field, value in result.items()
                    if field not in {"score", "raw_bm25", "matched_terms"}
                }
                merged[key]["rrf_score"] = 0.0
                merged[key]["ranks"] = {}
                merged[key]["original_scores"] = {}
            merged[key]["rrf_score"] += 1.0 / (k + rank)
            merged[key]["ranks"][retriever] = rank
            merged[key]["original_scores"][retriever] = result.get("score")
    return sorted(
        merged.values(),
        key=lambda item: (
            -item["rrf_score"],
            min(item["ranks"].values()),
            item["chunk_id"],
            # 跨目标融合时不同候选可以同名，再带上目标身份才是确定顺序。
            item.get("target_id") or "",
        ),
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
