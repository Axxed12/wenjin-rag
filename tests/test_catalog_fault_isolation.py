"""故障半径：一个身份不合法的语料目录不得让整个语料目录功能失效。

旧行为（本次修复前）是 `corpus_catalog` 扫到任一坏目录就对**整份列表**抛出，
于是 `find_corpus` 查任何一个正常语料也失败，`/files/...` 取件全部 503、
`list_corpora` 整体失败。

修复后的两条要求同时成立，缺一不可：
1. **隔离**：坏目录只影响它自己，正常语料照常列出、解析、取件；
2. **不放松**：身份可疑或不合法的语料永远不能被当作正常语料服务——
   不进 `corpora`、不被 `find_corpus` 命中、不能被下载。

第 2 条是安全属性，不能为了让第 1 条成立而牺牲。本文件对每种坏法都同时断言这两条。
"""

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from wenjin.access import AccessPolicy, AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.catalog import (
    CorpusCatalog,
    corpus_catalog,
    corpus_catalog_report,
    find_corpus,
)
from wenjin.corpus_identity import CorpusIdentityError

GOOD_ID = "gdal-3.13.1"
GOOD_PATH = "guide.md"
GOOD_BYTES = b"# GDAL guide\n"
GOOD_HASH = hashlib.sha256(GOOD_BYTES).hexdigest()


def write_manifest(root: Path, *, directory: str, declared, visibility: str = "corpora",
                   raw_data_subdir: str = "gdal/3.13.1", path: str = GOOD_PATH,
                   content: bytes | None = GOOD_BYTES) -> Path:
    """在指定目录名下写一份语料；declared 是 manifest 声明的 corpus_id。"""
    manifest_dir = root / visibility / directory
    manifest_dir.mkdir(parents=True, exist_ok=True)
    entry: dict = {"path": path, "retrieval_eligible": True, "indexable": True}
    if content is not None:
        raw_dir = root / "raw_data" / raw_data_subdir
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / path).write_bytes(content)
        entry.update({"raw_sha256": hashlib.sha256(content).hexdigest(),
                      "bytes": len(content)})
    manifest = {
        "schema_version": 3, "library": "gdal", "version": "3.13.1",
        "raw_data_subdir": raw_data_subdir, "license": "MIT", "files": [entry],
    }
    if declared is not None:
        manifest["corpus_id"] = declared
    manifest_path = manifest_dir / "source-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return manifest_path


def write_good_corpus(root: Path) -> Path:
    return write_manifest(root, directory=GOOD_ID, declared=GOOD_ID)


# 三种必测坏法 + 两种同族坏法（都会在旧实现里打穿整份目录）。
BAD_SHAPES = {
    "中文目录名": ("我的文档", "我的文档", "directory_name_invalid"),
    "带空格的目录名": ("has space", "has space", "directory_name_invalid"),
    "以符号开头": ("-leading", "-leading", "directory_name_invalid"),
    "目录名与声明不一致": ("private-1", "public-1", "identity_conflict"),
    "声明值非法": ("ok-1.0", "我的文档", "declared_id_invalid"),
}


@pytest.fixture(params=sorted(BAD_SHAPES))
def bad_shape(request):
    directory, declared, reason = BAD_SHAPES[request.param]
    return request.param, directory, declared, reason


# --- 1. 隔离：坏目录不再打穿整份目录 ---


def test_catalog_survives_one_bad_directory(tmp_path, bad_shape):
    """一个坏目录存在时，`corpus_catalog` 不抛错，正常语料条目完整。"""
    label, directory, declared, reason = bad_shape
    write_good_corpus(tmp_path)
    write_manifest(tmp_path, directory=directory, declared=declared)

    entries = corpus_catalog(tmp_path)
    assert [entry.corpus_id for entry in entries] == [GOOD_ID], label

    report = corpus_catalog_report(tmp_path)
    assert isinstance(report, CorpusCatalog)
    assert [problem.reason for problem in report.problems] == [reason], label
    assert report.problems[0].directory == directory, label


def test_find_corpus_unaffected_by_a_bad_directory(tmp_path, bad_shape):
    """查**正常**语料成功，查**坏**的那个明确失败。这是本次修复的核心。"""
    label, directory, declared, reason = bad_shape
    write_good_corpus(tmp_path)
    write_manifest(tmp_path, directory=directory, declared=declared)

    entry = find_corpus(tmp_path, GOOD_ID)
    assert entry is not None and entry.manifest_path == f"corpora/{GOOD_ID}/source-manifest.json"

    # 坏目录名必须明确失败，而不是被当成"不存在"。
    with pytest.raises(CorpusIdentityError, match="身份"):
        find_corpus(tmp_path, directory)

    # 它自称的 id 也**绝不能**返回一份语料。可以是不存在，也可以是明确失败，
    # 但绝不能是"查到了"——那正是 P1-1 要防的"顶着公开身份被服务"。
    try:
        found = find_corpus(tmp_path, declared)
    except CorpusIdentityError:
        found = None
    assert found is None, f"{label}: {declared} 不能作为正常语料被查到"


def test_bad_directory_claiming_a_live_corpus_id_does_not_hijack_it(tmp_path):
    """坏目录在 manifest 里自称某个正常语料的 id 时，正常语料照常服务。

    这是刻意选的故障边界：判定只看**目录名**，不看问题 manifest 自称的声明值
    （采信声明值正是 P1-1 漏洞）。否则任何一份放在非法目录名下的 manifest，
    只要写上 `osg-3.6.5`，就能让真正的 osg-3.6.5 停止服务。
    """
    write_good_corpus(tmp_path)
    write_manifest(tmp_path, directory="我的文档", declared=GOOD_ID, raw_data_subdir="bad/1")

    report = corpus_catalog_report(tmp_path)
    assert [entry.corpus_id for entry in report.entries] == [GOOD_ID]
    assert [problem.reason for problem in report.problems] == ["directory_name_invalid"]
    assert report.problems[0].declared_id == GOOD_ID, "自称值要留给运维定位"

    assert find_corpus(tmp_path, GOOD_ID) is not None, "正常语料不能被别人的自称值连坐"
    with pytest.raises(CorpusIdentityError):
        find_corpus(tmp_path, "我的文档")


def test_find_corpus_returns_none_for_a_genuinely_absent_id(tmp_path, bad_shape):
    """坏目录在场时，"确实不存在"仍然报不存在，而不是被牵连成失败。"""
    _, directory, declared, _ = bad_shape
    write_good_corpus(tmp_path)
    write_manifest(tmp_path, directory=directory, declared=declared)
    assert find_corpus(tmp_path, "no-such-corpus-9.9") is None


def test_duplicate_corpus_id_quarantines_both_copies(tmp_path):
    """两份目录声明同一身份：**两份**都被隔离，且查这个 id 失败。

    留任何一份在正常条目里，都等于让"请求落到哪一份"取决于遍历顺序。
    """
    write_manifest(tmp_path, directory="dup-1.0", declared="dup-1.0")
    write_manifest(tmp_path, directory="dup-1.0", declared="dup-1.0",
                   visibility="corpora-private", raw_data_subdir="dup/2")
    write_good_corpus(tmp_path)

    report = corpus_catalog_report(tmp_path)
    assert [entry.corpus_id for entry in report.entries] == [GOOD_ID]
    assert {problem.reason for problem in report.problems} == {"duplicate_corpus_id"}
    assert sorted(problem.directory for problem in report.problems) == ["dup-1.0", "dup-1.0"]
    assert len(report.problems) == 2, "两份都要报，不能只报后来那一份"

    with pytest.raises(CorpusIdentityError, match="身份"):
        find_corpus(tmp_path, "dup-1.0")
    assert find_corpus(tmp_path, GOOD_ID) is not None


def test_unreadable_and_structurally_invalid_manifests_are_quarantined(tmp_path):
    """坏 manifest（JSON 坏了、结构不对、corpus_id 不是字符串）同样只隔离自己。"""
    write_good_corpus(tmp_path)
    broken = tmp_path / "corpora/broken-1.0"
    broken.mkdir(parents=True)
    (broken / "source-manifest.json").write_text("{not json", encoding="utf-8")
    not_object = tmp_path / "corpora/list-1.0"
    not_object.mkdir(parents=True)
    (not_object / "source-manifest.json").write_text("[1,2]", encoding="utf-8")
    wrong_type = tmp_path / "corpora/wrongtype-1.0"
    wrong_type.mkdir(parents=True)
    (wrong_type / "source-manifest.json").write_text(
        json.dumps({"corpus_id": 123, "files": []}), encoding="utf-8")

    report = corpus_catalog_report(tmp_path)
    assert [entry.corpus_id for entry in report.entries] == [GOOD_ID]
    assert sorted(problem.reason for problem in report.problems) == [
        "declared_id_invalid", "manifest_invalid", "manifest_unreadable",
    ]
    assert find_corpus(tmp_path, GOOD_ID) is not None
    with pytest.raises(CorpusIdentityError):
        find_corpus(tmp_path, "wrongtype-1.0")


def test_problem_records_are_server_side_only(tmp_path):
    """问题记录只给运维面，且**没有**任何序列化入口可以把它接上网络接口。

    `CorpusProblem` 带的是原始目录名与路径（含 corpora-private/ 底下的），
    问题目录又不做权限过滤（身份未知，无法判定），所以一个 to_dict 就是一条泄露通道。
    这里把"不许有这个方法"固化成断言，避免以后有人顺手加回来。
    """
    write_manifest(tmp_path, directory="private-1", declared="public-1",
                   visibility="corpora-private")
    problem = corpus_catalog_report(tmp_path).problems[0]
    assert problem.declared_id == "public-1", "运维面要能看到自称值以定位问题"
    assert problem.location == "corpora-private/private-1/source-manifest.json"
    for forbidden in ("to_dict", "to_json", "as_dict", "payload"):
        assert not hasattr(problem, forbidden), f"CorpusProblem 不该有 {forbidden}"
    assert not hasattr(corpus_catalog_report(tmp_path), "problems_payload")


def test_a_third_directory_claiming_a_quarantined_id_is_never_served(tmp_path,
                                                                    monkeypatch):
    """同名目录出现**第三份**时，它也不能被当成正主收下。

    这条路径当前打不到：`_manifest_paths` 只扫两个固定父目录下的一层，同名目录最多两份。
    但"最多两份"是**另一个函数**的属性，不能把这里的正确性寄托在它身上——
    哪天真支持 `corpora/分组/<id>/` 的层级目录，`in owners` 判定在 pop 之后就会失效，
    第三份目录会被当正主收进 entries，等于攻击者凑够三份同名目录就能让自己的那份被服务。
    所以直接注入三份 manifest，绕开"当前扫不到"这个前提来验证加固本身。
    """
    from wenjin import catalog as catalog_module

    good_path = write_good_corpus(tmp_path)
    paths = [(good_path, "corpora")]
    for index, visibility in enumerate(("corpora", "corpora-private", "corpora")):
        paths.append((write_manifest(tmp_path, directory="dup-1.0", declared="dup-1.0",
                                     visibility=visibility,
                                     raw_data_subdir=f"dup/{index}"), visibility))

    monkeypatch.setattr(catalog_module, "_manifest_paths", lambda root: paths)
    report = catalog_module.corpus_catalog_report(tmp_path)

    assert [entry.corpus_id for entry in report.entries] == [GOOD_ID], \
        "第三份同名目录被当正主收下了"
    assert [problem.reason for problem in report.problems].count("duplicate_corpus_id") == 3
    with pytest.raises(CorpusIdentityError):
        catalog_module.find_corpus(tmp_path, "dup-1.0")


# --- 2. 不放松：坏目录永远不能被当成正常语料 ---


def build_server(tmp_path: Path, *, allowed: tuple[str, ...]):
    """构造服务；给坏身份也配上放行策略，确保拦截它的是身份判定而不是缺策略。"""
    from wenjin import mcp_server as module

    policies = {
        corpus_id: AccessPolicy(corpus_id, "public", ("*",)) for corpus_id in allowed
    }
    return module.build_mcp_server(
        SimpleNamespace(search=lambda *a, **k: {}),
        audit=SearchAudit(tmp_path / "audit.jsonl"),
        access=AccessRegistry(None, policies),
        raw_root=tmp_path / "raw_data",
        project_root=tmp_path,
        identity=module.RuntimeIdentity(GOOD_ID, "test", "gdal-3.13.1-test"),
    )


def fetch(app, url: str) -> int:
    return asyncio.run(fetch_response(app, url))["status"]


async def fetch_response(app, url: str) -> dict:
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        response = await client.get(url)
        return {"status": response.status_code, "text": response.text}


def test_bad_directory_does_not_break_download_of_a_good_corpus(tmp_path, bad_shape):
    """坏目录在场时，正常语料的 `/files/...` 仍返回 200；坏语料取不到。"""
    label, directory, declared, _ = bad_shape
    write_good_corpus(tmp_path)
    write_manifest(tmp_path, directory=directory, declared=declared,
                   raw_data_subdir="bad/1")
    # 三个身份都显式放行：这样非200只可能来自身份隔离，而不是缺策略的403。
    app = build_server(tmp_path, allowed=(GOOD_ID, directory, declared)).http_app(path="/mcp")

    assert fetch(app, f"/files/{GOOD_ID}/{GOOD_PATH}") == 200, label
    bad_status = fetch(app, f"/files/{directory}/{GOOD_PATH}")
    assert bad_status == 503, f"{label}: 坏目录应因身份问题被拒，实际 {bad_status}"
    # 自称的 id 取不到内容；是503（身份被拒）还是404（不算存在）都可以，但绝不能200。
    claimed_status = fetch(app, f"/files/{declared}/{GOOD_PATH}")
    assert claimed_status != 200, f"{label}: {declared} 不该取到任何内容"


def test_duplicate_corpus_id_does_not_break_download_of_a_good_corpus(tmp_path):
    """重复身份同样只让那一份取不到。"""
    write_good_corpus(tmp_path)
    write_manifest(tmp_path, directory="dup-1.0", declared="dup-1.0")
    write_manifest(tmp_path, directory="dup-1.0", declared="dup-1.0",
                   visibility="corpora-private", raw_data_subdir="dup/2")
    app = build_server(tmp_path, allowed=(GOOD_ID, "dup-1.0")).http_app(path="/mcp")

    assert fetch(app, f"/files/{GOOD_ID}/{GOOD_PATH}") == 200
    assert fetch(app, "/files/dup-1.0/guide.md") == 503


def test_list_corpora_does_not_leak_quarantined_directory_names(tmp_path, bad_shape):
    """`list_corpora` 不得外发被隔离目录的任何明细——连一个无权调用方也不行。

    被隔离的目录按定义不可服务，调用方对它没有任何可执行动作；而按ACL过滤救不了场：
    `sync_policies` 只为 `report.entries` 建策略，隔离目录没有策略，`registry.check`
    必然 default-deny，过滤后永远是空的，反而给出"一切正常"的假信号。
    所以明细整体不外发，只进服务端日志与本地CLI。这里对**整个响应**做全量字符串断言，
    模式同 tests/test_mcp_multicorpus.py::test_routed_drop_details_never_reach_the_client。
    """
    from fastmcp import Client

    from wenjin import mcp_server as module

    label, directory, declared, _ = bad_shape
    write_good_corpus(tmp_path)
    write_manifest(tmp_path, directory=directory, declared=declared, raw_data_subdir="bad/1")
    # 故意只放行正常语料：调用方一个隔离目录都无权读，仍然不该看到它们的名字。
    server = build_server(tmp_path, allowed=(GOOD_ID,))
    warnings: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            warnings.append(record.getMessage())

    handler = Capture()
    module.catalog_logger.addHandler(handler)
    try:
        async def scenario() -> dict:
            async with Client(server) as client:
                return (await client.call_tool("list_corpora", {})).structured_content

        data = asyncio.run(scenario())
    finally:
        module.catalog_logger.removeHandler(handler)

    assert [item["corpus_id"] for item in data["corpora"]] == [GOOD_ID], label
    assert data["count"] == 1
    blob = json.dumps(data, ensure_ascii=False)
    # 原因码是受控分类，允许出现；明细一律不行。
    for forbidden in (directory, declared, "corpora-private", "source-manifest",
                      "declared_id", "identity_problems", "我的文档", "public-1",
                      "private-1", "same-id", "ok-1.0", "leading"):
        assert forbidden not in blob, f"{label}: 响应里出现了 {forbidden!r}"
    # 但运维面必须拿得到：否则隔离就变成了静默丢弃。
    assert warnings, f"{label}: 被隔离的目录没有进服务端日志"
    assert any(directory in line for line in warnings), label
    assert any("reason=" in line for line in warnings), label


def test_a_broken_log_handler_cannot_fail_the_tool(tmp_path, bad_shape):
    """日志 handler 抛异常不能把一次成功的工具调用变成工具错误。

    实测过：一个 `emit` 里写错的 handler（多做了一次 % 格式化）会让 list_corpora 整个
    变成 `Error calling tool 'list_corpora'`——logging 只在 handler 自己调 handleError
    时才吞异常。运维日志不该有能力影响工具可用性，所以 log_identity_problems 逐条包了 try。
    """
    from fastmcp import Client

    from wenjin import mcp_server as module

    label, directory, declared, _ = bad_shape
    write_good_corpus(tmp_path)
    write_manifest(tmp_path, directory=directory, declared=declared, raw_data_subdir="bad/1")
    server = build_server(tmp_path, allowed=(GOOD_ID,))

    class Exploding(logging.Handler):
        def emit(self, record):
            raise RuntimeError("handler 坏了")

    exploding = Exploding()
    module.catalog_logger.addHandler(exploding)
    try:
        async def scenario() -> dict:
            async with Client(server) as client:
                return (await client.call_tool("list_corpora", {})).structured_content

        data = asyncio.run(scenario())
    finally:
        module.catalog_logger.removeHandler(exploding)

    assert [item["corpus_id"] for item in data["corpora"]] == [GOOD_ID], label
    assert data["count"] == 1, label


def test_download_503_body_carries_no_directory_name_or_declared_id(tmp_path, bad_shape):
    """取件 503 只回一句固定文案，不含目录名、声明值或路径。"""
    label, directory, declared, _ = bad_shape
    write_good_corpus(tmp_path)
    write_manifest(tmp_path, directory=directory, declared=declared, raw_data_subdir="bad/1")
    app = build_server(tmp_path,
                       allowed=(GOOD_ID, directory, declared)).http_app(path="/mcp")

    body = asyncio.run(fetch_response(app, f"/files/{directory}/{GOOD_PATH}"))
    assert body["status"] == 503, label
    assert body["text"] == json.dumps(
        {"error": "unavailable", "detail": "语料配置无效，请联系管理员"},
        ensure_ascii=False, separators=(",", ":"),
    ), label
    for forbidden in (directory, declared, "corpora", "manifest"):
        assert forbidden not in body["text"], f"{label}: 503 正文里出现了 {forbidden!r}"
