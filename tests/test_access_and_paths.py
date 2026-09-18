"""访问策略、路径安全和取件的单元测试。"""

import json
from pathlib import Path

import pytest

from wenjin.access import (
    AccessDenied,
    AccessPolicy,
    AccessRegistry,
    default_policy,
    load_policy,
    write_policy,
)
from wenjin.catalog import corpus_catalog, find_corpus
from wenjin.paths import UnsafePathError, resolve_within
from wenjin.serving import SourceLibrary, SourceNotFound, attach_download_paths, download_path_for

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


# --- 取件 ---


def test_source_library_strips_raw_prefix_and_reports_media_type(tmp_path):
    (tmp_path / "demo" / "1.0").mkdir(parents=True)
    (tmp_path / "demo" / "1.0" / "g.md").write_text("# t\n", encoding="utf-8")
    library = SourceLibrary(tmp_path)
    served = library.resolve("raw_data/demo/1.0/g.md")
    assert served.path.name == "g.md"
    assert served.media_type == "text/markdown" and served.inline is True
    assert library.resolve("demo/1.0/g.md").path == served.path


def test_source_library_raises_for_escape_and_missing(tmp_path):
    library = SourceLibrary(tmp_path)
    with pytest.raises(SourceNotFound):
        library.resolve("raw_data/../../outside.txt")
    with pytest.raises(SourceNotFound):
        library.resolve("raw_data/nope.md")


def test_download_path_keeps_corpus_and_strips_prefix():
    assert download_path_for("proj-9.8.1", "raw_data/proj/9.8.1/README.md") \
        == "/files/proj-9.8.1/proj/9.8.1/README.md"


def test_attach_download_paths_does_not_mutate_input():
    evidence = [{"source_path": "raw_data/a/1.0/x.md", "text": "t"}]
    enriched = attach_download_paths(evidence, "a-1.0")
    assert "download_path" not in evidence[0]
    assert enriched[0]["download_path"] == "/files/a-1.0/a/1.0/x.md"
    assert enriched[0]["corpus_id"] == "a-1.0"


# --- 语料目录 ---


def test_catalog_lists_public_and_internal_and_flags_index(monkeypatch, tmp_path):
    from wenjin import catalog as catalog_module

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

    catalog_module.PUBLIC_DIR, catalog_module.INTERNAL_DIR = "corpora", "corpora-private"
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
