"""返回层：命中 child 之后换成父块，以及**同一父块只能占一个名额**。

这是本设计最容易出错的地方：同一个父块会被多个 child 各自命中，如果不在融合之前
按父块合并，同一段正文就会占掉多个 top-K 名额，把本可以进来的其它证据挤出去——
结果只是"少了几条"，看起来并没有变差，属于难以察觉的退化。

这里不走 Qdrant 也不加载模型：`RetrievalService` 允许注入 `dense_search` /
`keyword_search`，于是候选集合完全可控。父块表用的是**真实** BM25 文件（由真实写入
函数生成），所以 `resolve_parents` 走的是真实取父块路径。
"""

from types import SimpleNamespace

import pytest

from tests.parent_child_fixtures import build_parented_assets, chunk_payload
from tests.parent_child_probe import evidence_violations
from wenjin.retrieval.fusion import rrf_fuse
from wenjin.retrieval.service import RetrievalService, SearchConfig

QUERY = "line abc"
RRF_K = 60


def point(payload: dict, score: float):
    """向量检索返回的点：只需要 score 与 payload。"""
    return SimpleNamespace(score=score, payload=dict(payload))


def build_service(assets: dict, *, dense: list, keyword: list) -> RetrievalService:
    return RetrievalService(
        qdrant_client=SimpleNamespace(close=lambda: None),
        embedder=None,
        bm25_path=assets["bm25_path"],
        source_urls={},
        collection_name="synth_parented",
        supported_version="3.6.5",
        dense_search=lambda client, embedder, collection, query, top_k: dense[:top_k],
        keyword_search=lambda path, query, top_k: keyword[:top_k],
    )


def candidates(assets: dict, *, parent_children: int = 3, others: int = 4):
    """构造候选：同一个父块的若干 child 排在前面，后面跟几个**分属不同父块**的 child。

    后面这几个必须来自不同父块，否则它们自己也会互相合并且数不满 top-5，
    "名额有没有被浪费"就看不出来了。
    """
    groups = {
        candidate.chunk_id: [c for c in assets["children"] if c.parent_id == candidate.chunk_id]
        for candidate in assets["parents"]
    }
    parent = next(
        (candidate for candidate in assets["parents"]
         if len(groups[candidate.chunk_id]) >= parent_children),
        None,
    )
    if parent is None:
        pytest.skip("夹具里没有多 child 的父块，去重场景构造不出来")
    members = groups[parent.chunk_id]
    independent = []
    for candidate in assets["parents"]:
        if candidate.chunk_id == parent.chunk_id:
            continue
        independent.append(groups[candidate.chunk_id][0])
        if len(independent) == others:
            break
    assert len(independent) == others, "分属不同父块的独立块不够，名额占用的对照不成立"
    return parent, members, independent


def search(assets: dict, *, dense: list, keyword: list) -> dict:
    config = SearchConfig(version="3.6.5", candidate_k=20, top_k=5, rrf_k=RRF_K)
    return build_service(assets, dense=dense, keyword=keyword).search(QUERY, config=config)


@pytest.fixture()
def assets(tmp_path):
    # max_chars 调小是为了多切出几个父子组：名额占用必须有多条**互不相同**的候选
    # 才能看出来，一个只有两三个父块的语料证明不了"去重没有浪费名额"。
    return build_parented_assets(tmp_path, max_chars=60)


def test_hit_child_is_replaced_by_its_parent(assets):
    """命中的是 child，返回的证据必须是父块：行区间、正文、chunk_id 都换成父块的。"""
    parent, members, independents = candidates(assets)
    dense = [point(chunk_payload(members[0]), 0.9)]
    keyword = [chunk_payload(members[1])]
    evidence = search(assets, dense=dense, keyword=keyword)["evidence"]
    assert [item["chunk_id"] for item in evidence] == [parent.chunk_id]
    assert evidence[0]["text"] == parent.text
    assert (evidence[0]["start_line"], evidence[0]["end_line"]) == \
           (parent.start_line, parent.end_line)
    assert evidence[0]["text"] != members[0].text, "返回的还是 child，没换成父块"


def test_parent_text_is_the_source_slice_not_a_stitch_of_children(assets):
    """父块正文必须逐字等于它声明的原文区间，不能是 child 文本的拼接产物。"""
    parent, members, _ = candidates(assets)
    lines = assets["document"]["text"].splitlines(keepends=True)
    expected = "".join(lines[parent.start_line - 1 : parent.end_line])
    evidence = search(
        assets, dense=[point(chunk_payload(members[0]), 0.9)],
        keyword=[chunk_payload(members[1])],
    )["evidence"]
    assert evidence[0]["text"] == expected, "证据正文与它声明的行区间不是同一串字节"


def test_one_parent_hit_by_three_children_occupies_one_slot(assets):
    """**核心用例**：同一父块被三个 child 命中时，它只占一个名额。

    三个 child 排在两路检索的最前面，后面跟着四个各自独立的块。去重若失效，
    父块会以三条证据出现、把后面两条挤出去——总量看起来没变，证据却少了。
    """
    parent, members, independents = candidates(assets, parent_children=3, others=4)
    dense = [point(chunk_payload(members[0]), 0.9),
             point(chunk_payload(members[1]), 0.8),
             point(chunk_payload(members[2]), 0.7)]
    dense += [point(chunk_payload(chunk), 0.5) for chunk in independents]
    keyword = [chunk_payload(members[0])]
    keyword += [chunk_payload(chunk) for chunk in independents[:2]]

    evidence = search(assets, dense=dense, keyword=keyword)["evidence"]
    returned = [item["chunk_id"] for item in evidence]

    assert returned.count(parent.chunk_id) == 1, f"父块占了不止一个名额：{returned}"
    assert len(returned) == len(set(returned)), f"返回结果里有重复：{returned}"
    assert len(evidence) == 5, f"top-5 没有填满，名额被浪费了：{returned}"
    # 四个独立 child 各自换成自己的父块后仍应全部在场：被挤出去的不能是它们。
    expected = {parent.chunk_id, *(chunk.parent_id for chunk in independents)}
    assert set(returned) == expected, returned


def test_the_best_childs_rank_is_what_counts(assets):
    """分数取**最好的那个 child** 的排名，不能靠"命中得多"把分数堆上去。

    同一路里同一个父块出现多次时只记最好名次：按次数累加的话，一段正文可以仅凭
    "被切得碎、命中次数多"就压过别的证据。
    """
    parent, members, independents = candidates(assets, parent_children=3, others=4)
    dense = [point(chunk_payload(members[0]), 0.9),
             point(chunk_payload(members[1]), 0.8),
             point(chunk_payload(members[2]), 0.7)]
    keyword = [chunk_payload(members[0]), chunk_payload(members[1])]
    evidence = search(assets, dense=dense, keyword=keyword)["evidence"]
    top = next(item for item in evidence if item["chunk_id"] == parent.chunk_id)
    expected = 1.0 / (RRF_K + 1) + 1.0 / (RRF_K + 1)
    assert top["rrf_score"] == pytest.approx(expected), \
        f"父块分数 {top['rrf_score']} 不等于最好名次应得的 {expected}（疑似按命中次数累加）"
    assert top["retrieval_ranks"] == {"dense": 1, "bm25": 1}, top["retrieval_ranks"]


def test_without_parent_identity_the_slots_really_would_be_wasted(assets):
    """对照：把身份换回 `chunk_id`（改动前的行为），同一批候选就会白占名额。

    没有这条，上面那条"只占一个名额"的断言可能只是因为候选本来就不重复而成立。
    """
    parent, members, _ = candidates(assets, parent_children=3, others=4)
    # 三个候选都指向同一个父块，但各自还带着自己 child 的 chunk_id——这正是
    # `resolve_parents` 之后的形态：正文与区间已是父块的，身份字段还在。
    resolved = [
        {**chunk_payload(member), "parent_id": parent.chunk_id, "score": 0.9 - index * 0.1,
         "start_line": parent.start_line, "end_line": parent.end_line, "text": parent.text}
        for index, member in enumerate(members[:3])
    ]
    by_parent = rrf_fuse({"dense": resolved}, k=RRF_K)
    by_chunk = rrf_fuse({"dense": resolved}, k=RRF_K, identity=lambda item: item["chunk_id"])
    assert len(by_parent) == 1, "按父块身份应当合成一条"
    assert len(by_chunk) == 3, "按 chunk_id 身份本应散成三条——这正是要避免的退化"


def test_returned_evidence_passes_the_position_deduplication_check(assets):
    """返回结果要过返回层判定：无重复位置、无父子同时占位、名次连续。"""
    parent, members, independents = candidates(assets)
    dense = [point(chunk_payload(members[0]), 0.9),
             point(chunk_payload(members[1]), 0.8)]
    dense += [point(chunk_payload(chunk), 0.5) for chunk in independents]
    evidence = search(assets, dense=dense, keyword=[])["evidence"]
    assert evidence_violations(evidence) == {
        "duplicate_evidence": [], "same_position_different_text": [],
        "containment_within_result": [], "rank_sequence_broken": [],
    }


def test_hits_are_ranked_by_the_child_that_won(assets):
    """命中原因要能查：证据里保留"是哪个 child 把它召回的"。"""
    parent, members, _ = candidates(assets)
    evidence = search(
        assets, dense=[point(chunk_payload(members[0]), 0.9)],
        keyword=[chunk_payload(members[1])],
    )["evidence"]
    assert evidence[0]["chunk_id"] == parent.chunk_id


def test_a_child_whose_parent_is_missing_fails_loudly(tmp_path):
    """子块引用的父块取不到时**必须报错**，不能悄悄退回 child。

    退回 child 会让证据悄悄少掉上下文，看上去还是正常的检索结果——这正是最该避免的
    静默退化。资产损坏要在查询时立刻暴露，而不是让用户拿到半截证据。
    """
    assets = build_parented_assets(tmp_path, max_chars=60)
    parent, members, _ = candidates(assets)
    from tests.parent_child_fixtures import edit_sqlite

    edit_sqlite(assets["bm25_path"], [
        ("DELETE FROM parents WHERE chunk_id = ?", (parent.chunk_id,)),
    ])
    with pytest.raises(ValueError, match="取不到的父块"):
        search(assets, dense=[point(chunk_payload(members[0]), 0.9)], keyword=[])


def test_non_parented_index_is_untouched(assets):
    """没有 `parent_id` 的候选（老索引）行为完全不变：不读父块表、不去重。"""
    independents = assets["children"][:3]
    stripped = [
        {key: value for key, value in chunk_payload(chunk).items() if key != "parent_id"}
        for chunk in independents
    ]
    evidence = search(
        assets, dense=[point(payload, 0.9) for payload in stripped], keyword=[],
    )["evidence"]
    assert [item["chunk_id"] for item in evidence] == [chunk.chunk_id for chunk in independents]
