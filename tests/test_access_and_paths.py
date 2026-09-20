"""访问策略、路径安全和取件的单元测试。"""

import hashlib
import json
from pathlib import Path

import pytest

from wenjin.access import (
    AccessDenied,
    AccessPolicy,
    AccessRegistry,
    AccessSnapshotError,
    default_policy,
    load_policy,
    load_snapshot,
    write_policy,
)
from wenjin.catalog import corpus_catalog, find_corpus, index_target_configs
from wenjin.paths import UnsafePathError, resolve_within
from wenjin.serving import (
    CorpusSources,
    SourceModified,
    SourceNotFound,
    attach_download_paths,
    download_path_for,
)

# --- 访问策略 ---


def test_policy_denies_by_default_when_absent():
    """没有策略文件就是拒绝，不是放行。"""
    registry = AccessRegistry(None, {})
    assert registry.check("anything", "agent-a") is False
    with pytest.raises(AccessDenied, match="没有为语料"):
        registry.require("anything", "agent-a")


def test_policy_wildcard_allows_even_without_identity():
    """`*`是"公开"的显式声明，未配置Bearer的部署也要能读公开语料。"""
    policy = AccessPolicy("pub", "public", ("*",))
    assert policy.allows("agent-a") is True
    assert policy.allows(None) is True


def test_internal_policy_rejects_anonymous_and_unlisted():
    policy = AccessPolicy("internal", "internal", ("agent-a",))
    assert policy.allows("agent-a") is True
    assert policy.allows("agent-b") is False
    assert policy.allows(None) is False


def test_default_policy_for_internal_corpus_denies_everyone():
    """新建内部语料先拒绝所有人：宁可暂时没人能读，也不能默认开放。"""
    policy = default_policy("internal-1.0", "internal")
    assert policy.allowed_clients == ()
    assert policy.allows("agent-a") is False


def test_policy_roundtrip_and_corpus_id_mismatch(tmp_path):
    policy = AccessPolicy("demo-1.0", "internal", ("agent-a",), "note")
    path = write_policy(tmp_path, policy)
    assert load_policy(tmp_path, "demo-1.0") == policy
    data = json.loads(path.read_text(encoding="utf-8"))
    data["corpus_id"] = "other"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="corpus_id与文件名不一致"):
        load_policy(tmp_path, "demo-1.0")


def test_registry_visible_corpora_filters_by_client():
    registry = AccessRegistry(None, {
        "pub": AccessPolicy("pub", "public", ("*",)),
        "internal": AccessPolicy("internal", "internal", ("agent-a",)),
    })
    assert registry.visible_corpora("agent-a") == ["internal", "pub"]
    assert registry.visible_corpora("agent-b") == ["pub"]


def test_snapshot_fails_closed_on_corrupt_policy(tmp_path):
    """策略文件损坏必须整体失败，不能跳过坏文件继续用其余策略。"""
    write_policy(tmp_path, AccessPolicy("good-1.0", "public", ("*",)))
    (tmp_path / "broken-1.0.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(AccessSnapshotError):
        load_snapshot(tmp_path)


def test_snapshot_fails_closed_on_policy_identity_conflict(tmp_path):
    write_policy(tmp_path, AccessPolicy("good-1.0", "public", ("*",)))
    (tmp_path / "mismatch-1.0.json").write_text(
        json.dumps({"schema_version": 1, "corpus_id": "other", "visibility": "public",
                    "allowed_clients": ["*"]}), encoding="utf-8"
    )
    with pytest.raises(AccessSnapshotError):
        load_snapshot(tmp_path)


def test_snapshot_reflects_revocation_immediately(tmp_path):
    """撤权后新取的快照必须立刻生效——这正是不能缓存旧快照的原因。"""
    write_policy(tmp_path, AccessPolicy("c-1.0", "internal", ("agent-a",)))
    assert load_snapshot(tmp_path).check("c-1.0", "agent-a") is True
    write_policy(tmp_path, AccessPolicy("c-1.0", "internal", ()))
    assert load_snapshot(tmp_path).check("c-1.0", "agent-a") is False


# --- 路径安全 ---


@pytest.mark.parametrize("relative", ["../secret.txt", "a/../../secret.txt", "/etc/passwd",
                                      "C:/Windows/win.ini", "", "   "])
def test_resolve_within_rejects_unsafe_paths(tmp_path, relative):
    with pytest.raises(UnsafePathError):
        resolve_within(tmp_path, relative)


def test_resolve_within_accepts_normal_relative_path(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.md").write_text("x", encoding="utf-8")
    assert resolve_within(tmp_path, "a/b.md") == (tmp_path / "a" / "b.md").resolve()


def test_resolve_within_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    # 解析后的真实路径已在根目录之外，会被越界检查先拦下；两条防线都算通过。
    with pytest.raises(UnsafePathError, match="超出根目录|符号链接"):
        resolve_within(root, "link/secret.txt")


# --- 取件：路径适配与字节校验 ---


def make_corpus(tmp_path, *, corpus_id="demo-1.0", library="demo", version="1.0",
                subdir="demo/1.0", files=None):
    """搭一个语料：manifest登记记录 + raw_data下的原文。"""
    if files is None:
        files = [("guide.md", b"# Guide\n", True)]
    raw_dir = tmp_path / "raw_data" / subdir
    raw_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, content, eligible in files:
        target = raw_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        entries.append({
            "path": name,
            "raw_sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
            "retrieval_eligible": eligible,
            "indexable": eligible,
        })
    manifest_dir = tmp_path / "corpora" / corpus_id
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "source-manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 3, "corpus_id": corpus_id, "library": library, "version": version,
        "raw_data_subdir": subdir, "license": "MIT", "files": entries,
    }), encoding="utf-8")
    return CorpusSources.from_manifest(
        manifest_path, raw_root=tmp_path / "raw_data", expected_corpus_id=corpus_id
    )


def test_path_adapter_accepts_osg_bare_paths(tmp_path):
    """OSG的source_path是裸的`include/osg/Node`，必须能映射到manifest登记路径。"""
    sources = make_corpus(
        tmp_path, corpus_id="osg-3.6.5", library="osg", version="3.6.5", subdir="osg/3.6.5",
        files=[("include/osg/Node", b"class Node;\n", True)],
    )
    record = sources.match_chunk_path("include/osg/Node")
    assert record.relative == "include/osg/Node"
    assert sources.locate(record.relative).name == "Node"


def test_path_adapter_accepts_prefixed_paths(tmp_path):
    """新语料的source_path带`raw_data/<subdir>/`前缀，也要映射到同一份登记路径。"""
    sources = make_corpus(tmp_path, files=[("docs/a.md", b"a\n", True)])
    assert sources.match_chunk_path("raw_data/demo/1.0/docs/a.md").relative == "docs/a.md"


def test_path_adapter_rejects_other_corpus_and_unregistered(tmp_path):
    sources = make_corpus(tmp_path, files=[("docs/a.md", b"a\n", True)])
    with pytest.raises(SourceNotFound, match="不属于该语料"):
        sources.match_chunk_path("raw_data/other/2.0/docs/a.md")
    with pytest.raises(SourceNotFound, match="未登记"):
        sources.match_chunk_path("raw_data/demo/1.0/docs/missing.md")
    with pytest.raises(SourceNotFound, match="未登记"):
        sources.match_chunk_path("docs/missing.md")


def test_read_verified_returns_the_verified_bytes(tmp_path):
    sources = make_corpus(tmp_path, files=[("docs/a.md", b"hello\n", True)])
    verified = sources.read_verified("docs/a.md")
    assert verified.content == b"hello\n"
    assert verified.media_type == "text/markdown"
    assert verified.response_headers()["X-Content-Type-Options"] == "nosniff"


def test_read_verified_rejects_tampered_file(tmp_path):
    """语料被改动过就不能当原文送出：登记Hash和实际字节必须一致。"""
    sources = make_corpus(tmp_path, files=[("docs/a.md", b"hello\n", True)])
    (tmp_path / "raw_data" / "demo" / "1.0" / "docs" / "a.md").write_bytes(b"tampered\n")
    with pytest.raises(SourceModified, match="不符"):
        sources.read_verified("docs/a.md")


def test_read_verified_rejects_unregistered_and_missing(tmp_path):
    sources = make_corpus(tmp_path, files=[("docs/a.md", b"a\n", True)])
    with pytest.raises(SourceNotFound, match="未登记"):
        sources.read_verified("docs/other.md")
    (tmp_path / "raw_data" / "demo" / "1.0" / "docs" / "a.md").unlink()
    with pytest.raises(SourceNotFound, match="没有这个文件"):
        sources.read_verified("docs/a.md")


def test_listing_includes_archived_files(tmp_path):
    """下载清单用manifest全部登记文件：归档件不进检索，但仍应能下载。"""
    sources = make_corpus(tmp_path, files=[("a.md", b"a\n", True), ("b.zip", b"PK", False)])
    assert [item.relative for item in sources.listing()] == ["a.md", "b.zip"]


def test_attach_download_paths_does_not_relabel_foreign_evidence(tmp_path):
    """其他语料的证据必须丢弃并报告，不能重标成当前语料。"""
    sources = make_corpus(tmp_path, files=[("docs/a.md", b"a\n", True)])
    evidence = [
        {"chunk_id": "own", "source_path": "raw_data/demo/1.0/docs/a.md"},
        {"chunk_id": "foreign", "source_path": "raw_data/other/2.0/docs/b.md",
         "corpus_id": "other-2.0"},
        {"chunk_id": "unregistered", "source_path": "raw_data/demo/1.0/docs/zzz.md"},
    ]
    result = attach_download_paths(evidence, sources)
    assert [item["chunk_id"] for item in result.evidence] == ["own"]
    assert {item["chunk_id"] for item in result.dropped} == {"foreign", "unregistered"}
    assert result.evidence[0]["corpus_id"] == "demo-1.0"
    assert result.evidence[0]["download_path"] == "/files/demo-1.0/docs/a.md"


def test_attach_download_paths_preserves_own_corpus_id(tmp_path):
    """证据自带的corpus_id与当前语料一致时原样保留，不覆盖。"""
    sources = make_corpus(tmp_path, files=[("docs/a.md", b"a\n", True)])
    evidence = [{"chunk_id": "x", "source_path": "docs/a.md", "corpus_id": "demo-1.0"}]
    result = attach_download_paths(evidence, sources)
    assert result.evidence[0]["corpus_id"] == "demo-1.0"


def test_download_path_encodes_segments():
    """路径分段编码：空格、#、? 和中文都不能原样拼进URL。"""
    path = download_path_for("demo-1.0", "docs/a b#c?d/中文.md")
    assert path == "/files/demo-1.0/docs/a%20b%23c%3Fd/%E4%B8%AD%E6%96%87.md"
    assert "#" not in path and "?" not in path


def test_response_headers_encode_filename_rfc5987(tmp_path):
    """文件名用filename*编码，不把中文或引号手拼进响应头。"""
    sources = make_corpus(tmp_path, files=[("docs/中文 文件.md", b"x\n", True)])
    disposition = sources.read_verified("docs/中文 文件.md").response_headers()[
        "Content-Disposition"
    ]
    assert "filename*=UTF-8''" in disposition
    assert '"' not in disposition.split("filename*=")[1]


# --- 语料目录 ---


def test_catalog_lists_public_and_internal_and_flags_index(tmp_path):
    for directory, corpus_id, library in (("corpora", "pub-1.0", "pub"),
                                          ("corpora-private", "int-1.0", "int")):
        manifest_dir = tmp_path / directory / corpus_id
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "source-manifest.json").write_text(json.dumps({
            "corpus_id": corpus_id, "library": library, "version": "1.0",
            "license": "MIT", "raw_data_subdir": f"{library}/1.0",
            "files": [{"path": "a.md", "retrieval_eligible": True},
                      {"path": "b.zip", "retrieval_eligible": False}],
        }), encoding="utf-8")
    config_dir = tmp_path / "configs" / "indexes"
    config_dir.mkdir(parents=True)
    (config_dir / "pub-1.0.json").write_text(json.dumps({
        "target_id": "pub-1.0-blocks-split",
        "manifest_path": "corpora/pub-1.0/source-manifest.json",
    }), encoding="utf-8")

    entries = corpus_catalog(tmp_path)
    assert [entry.corpus_id for entry in entries] == ["int-1.0", "pub-1.0"]
    public = find_corpus(tmp_path, "pub-1.0")
    assert public.visibility == "public" and public.indexed is True
    assert public.retrieval_eligible_files == 1
    assert find_corpus(tmp_path, "int-1.0").indexed is False


def test_catalog_reads_real_project():
    """真实项目目录也要能读出来，避免只在合成夹具上通过。"""
    root = Path(__file__).resolve().parents[1]
    entries = corpus_catalog(root)
    assert any(entry.corpus_id == "osg-3.6.5" for entry in entries)


def test_catalog_counts_scope_by_each_corpus_own_semantics(tmp_path):
    """OSG 用 `indexable`，新语料用 `retrieval_eligible`，两者都要按自己的语义计数。

    只数 `retrieval_eligible` 会把 OSG 报成"0 个可检索文件"——它有 29 个。
    """
    (tmp_path / "corpora/osg-3.6.5").mkdir(parents=True)
    (tmp_path / "corpora/osg-3.6.5/source-manifest.json").write_text(json.dumps({
        "schema_version": 3, "corpus_id": "osg-3.6.5", "library": "osg", "version": "3.6.5",
        "raw_data_subdir": "osg/3.6.5", "license": "OSGPL",
        "files": [
            # legacy 语义：没有 retrieval_eligible 字段，靠 indexable
            {"path": "a", "raw_sha256": "0" * 64, "indexable": True},
            {"path": "b", "raw_sha256": "0" * 64, "indexable": True},
            {"path": "c", "raw_sha256": "0" * 64, "indexable": False},
        ],
    }), encoding="utf-8")
    (tmp_path / "corpora/new-1.0").mkdir(parents=True)
    (tmp_path / "corpora/new-1.0/source-manifest.json").write_text(json.dumps({
        "schema_version": 3, "corpus_id": "new-1.0", "library": "new", "version": "1.0",
        "raw_data_subdir": "new/1.0", "license": "test",
        "files": [
            {"path": "x", "raw_sha256": "0" * 64, "retrieval_eligible": True},
            {"path": "y", "raw_sha256": "0" * 64, "retrieval_eligible": False},
        ],
    }), encoding="utf-8")

    osg = find_corpus(tmp_path, "osg-3.6.5")
    assert osg.retrieval_eligible_files == 2, "legacy indexable 语义没有被计入"
    assert osg.scope_field == "indexable"
    assert osg.retrieval_scope_files == ("a", "b")

    new = find_corpus(tmp_path, "new-1.0")
    assert new.retrieval_eligible_files == 1
    assert new.scope_field == "retrieval_eligible"


def test_catalog_real_osg_reports_nonzero_scope():
    """真实 OSG manifest 不能在目录里显示成 0 个可检索文件。"""
    root = Path(__file__).resolve().parents[1]
    osg = find_corpus(root, "osg-3.6.5")
    assert osg is not None
    assert osg.retrieval_eligible_files > 0
    assert osg.scope_field == "indexable"


def test_catalog_distinguishes_config_from_usable():
    """`indexed` 只表示有配置提到它；能不能用要另外判。"""
    root = Path(__file__).resolve().parents[1]
    osg = find_corpus(root, "osg-3.6.5")
    assert osg.indexed is True
    assert osg.to_dict()["has_index_config"] is True
    assert osg.to_dict()["index_targets"]


def test_conflicting_duplicate_target_config_is_rejected(tmp_path):
    """同一个 target_id 在两份配置里内容冲突时必须报错。

    静默取第一份会让"这个 target 指向哪个 collection"取决于文件遍历顺序，
    调用方完全看不出来。
    """
    config_dir = tmp_path / "configs/indexes"
    config_dir.mkdir(parents=True)
    base = {"target_id": "dup-1", "manifest_path": "corpora/a/source-manifest.json",
            "dense_collection": "coll_a"}
    (config_dir / "a.json").write_text(json.dumps(base), encoding="utf-8")
    (config_dir / "b.json").write_text(
        json.dumps({**base, "dense_collection": "coll_b"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="内容冲突"):
        index_target_configs(tmp_path)


def test_identical_duplicate_target_config_is_deduplicated(tmp_path):
    """同一内容出现在单份配置和路由清单里是正常的，应当去重而不是报错。"""
    config_dir = tmp_path / "configs/indexes"
    config_dir.mkdir(parents=True)
    entry = {"target_id": "same-1", "manifest_path": "corpora/a/source-manifest.json",
             "dense_collection": "coll_a"}
    (config_dir / "single.json").write_text(json.dumps(entry), encoding="utf-8")
    (config_dir / "route.json").write_text(
        json.dumps({"schema_version": 1, "candidates": [entry]}), encoding="utf-8"
    )
    configs = index_target_configs(tmp_path)
    assert list(configs) == ["same-1"]
    assert configs["same-1"]["dense_collection"] == "coll_a"


def test_real_project_index_configs_have_no_duplicate_target_ids():
    """真实项目里同一个 target 会同时出现在路由清单和自己的配置文件里，必须能通过。"""
    root = Path(__file__).resolve().parents[1]
    configs = index_target_configs(root)
    assert configs["osg-3.6.5-blocks-dualhash-003"]["dense_collection"] == (
        "osg_3_6_5_dense_blocks_dualhash"
    )
    assert len(configs) == 11
