"""Reciprocal Rank Fusion：只使用各检索器的排名融合候选。"""

from collections.abc import Callable


def identity_by_parent(result: dict):
    """默认身份：有 `parent_id` 时按父块合并，否则按 `chunk_id`。

    父子化索引里同一父块会由多个 child 命中，必须合成一条候选，否则同一段正文会占掉
    多个名额。没有 `parent_id` 的候选（fixed/blocks/blocks-split）行为完全不变。
    """
    return result.get("parent_id") or result["chunk_id"]


def rrf_fuse(
    result_sets: dict[str, list[dict]],
    *,
    k: int = 60,
    identity: Callable[[dict], object] | None = None,
) -> list[dict]:
    """合并多个排名列表；rank 从 1 开始，重复出现的候选按名次累加。

    `identity` 决定"两条候选是不是同一个"，默认 `identity_by_parent`——在**单个**索引内
    `chunk_id` 唯一，父子化索引则要按 `parent_id` 合并。跨索引融合时必须传入包含目标
    身份的键，否则两个目标里同名的 chunk 会被并成一条：正文取先出现的那份，分数却
    叠加了双方的排名贡献。

    键只在**同一份候选列表内部**需要唯一；不同检索器之间用同一个键表示"同一条候选"，
    正是靠它合并且分别记录各自的名次。同一路列表里同一个键**重复出现**时只记最好的
    名次、不重复累加：父子化之后一个父块会由多个 child 各自命中，若按次数累加，
    同一段正文就能靠"命中得多"挤掉别的证据。
    """
    if k < 0:
        raise ValueError("RRF k must be non-negative")
    key_of = identity or identity_by_parent
    merged: dict[object, dict] = {}
    # 每个键在每一路里的最好名次；RRF 分数最后用它一次性算出来，避免先加后减的浮点误差。
    best_ranks: dict[object, dict[str, int]] = {}
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
                best_ranks[key] = {}
            previous = best_ranks[key].get(retriever)
            if previous is not None and rank >= previous:
                continue
            best_ranks[key][retriever] = rank
            merged[key]["ranks"][retriever] = rank
            merged[key]["original_scores"][retriever] = result.get("score")
    for key, item in merged.items():
        item["rrf_score"] = sum(1.0 / (k + rank) for rank in best_ranks[key].values())
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
