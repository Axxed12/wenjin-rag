"""第六批：发布隔离——镜像与仓库各该带什么、不该带什么。

这些是**规则本身**的测试，不是"跑一遍看看"：镜像里带了 ACL 或漏了索引配置，
都不会有别的测试发现。
"""

import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from wenjin.access import AccessSnapshotError, load_snapshot
from wenjin.catalog import INTERNAL_INDEX_DIR, PUBLIC_INDEX_DIR, index_config_dir
from wenjin.index_registry import IndexTarget

ROOT = Path(__file__).resolve().parents[1]

# 部署配置与内部命名，绝不能进构建上下文。
MUST_BE_EXCLUDED_FROM_IMAGE = (
    "configs/access",
    "corpora-private",
    "configs/indexes-private",
)


def read_ignore_file(name: str) -> list[str]:
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def dockerfile_copy_sources() -> list[str]:
    """Dockerfile 里 COPY 的来源路径（去掉 --from 阶段拷贝）。"""
    sources = []
    for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        match = re.match(r"COPY\s+(?:--\S+\s+)*(.+)$", line.strip())
        if not match or "--from=" in line:
            continue
        parts = match.group(1).split()
        sources.extend(parts[:-1])  # 最后一个是目标路径
    return [source for source in sources if source not in {".", "./"}]


def matched_by_ignore_file(name: str, path: str) -> bool:
    """按忽略文件语义判断：路径自身或任一祖先被某条模式命中即排除。"""
    patterns = read_ignore_file(name)
    parts = Path(path).parts
    candidates = ["/".join(parts[: index + 1]) for index in range(len(parts))]
    candidates.append(parts[-1])
    return any(
        fnmatch.fnmatch(candidate, pattern.rstrip("/"))
        for candidate in candidates
        for pattern in patterns
    )


def excluded_from_image(path: str) -> bool:
    return matched_by_ignore_file(".dockerignore", path)


def excluded_from_git(path: str) -> bool:
    return matched_by_ignore_file(".gitignore", path)


def test_dockerignore_excludes_deployment_secrets():
    """ACL 里有 client_id 与内部 corpus_id；镜像分发出去就等于把它们带出去。"""
    for path in MUST_BE_EXCLUDED_FROM_IMAGE:
        assert excluded_from_image(path), f"{path} 没有被 .dockerignore 排除"


def test_dockerignore_does_not_exclude_what_the_image_needs():
    """反向检查：排除过头会让镜像缺文件，而这种失败只有在构建后才发现。

    真实构建被禁止（开发期不重建镜像），所以这条不变量只能靠静态检查守住。
    """
    copied = dockerfile_copy_sources()
    assert copied, "没有解析到任何 COPY 来源，检查正则是否失效"
    for source in copied:
        assert not excluded_from_image(source), f"Dockerfile 需要的 {source} 被排除了"


def test_dockerfile_does_not_copy_private_material():
    for source in dockerfile_copy_sources():
        assert "corpora-private" not in source
        assert "configs/access" not in source


def test_gitignore_excludes_private_index_configs():
    patterns = read_ignore_file(".gitignore")
    assert "corpora-private/" in patterns
    assert "configs/indexes-private/" in patterns


def test_index_config_dir_follows_visibility(tmp_path):
    assert index_config_dir(tmp_path, visibility="public") == tmp_path / PUBLIC_INDEX_DIR
    assert index_config_dir(tmp_path, visibility="internal") == tmp_path / INTERNAL_INDEX_DIR


def test_real_project_keeps_private_configs_out_of_the_tracked_directory():
    """已提交目录里的每个索引目标都必须指向公开语料。

    只忽略 `corpora-private/` 是不够的：内部语料的索引配置默认写在
    `configs/indexes/`，那条路会绕过忽略规则把内部 corpus_id 提交上去。
    """
    config_dir = ROOT / PUBLIC_INDEX_DIR
    offenders = []
    for path in sorted(config_dir.glob("*.json")):
        text = path.read_text(encoding="utf-8-sig")
        if "corpora-private" in text:
            offenders.append(path.name)
    assert not offenders, f"可提交目录里出现指向非公开语料的配置：{offenders}"


def test_missing_access_directory_denies_everything(tmp_path):
    """容器里不带 ACL 时必须是"默认拒绝所有人"，而不是"没有策略就放行"。

    这条把"镜像不含 configs/access"从一个部署细节变成一个有测试守住的性质。
    """
    for directory in (tmp_path / "no-such-dir", tmp_path / "empty"):
        directory.mkdir(exist_ok=True)
        registry = load_snapshot(directory)
        assert registry.check("osg-3.6.5", "any-client") is False


def test_access_snapshot_error_is_fail_closed(tmp_path):
    """损坏的策略文件整体失败，不能跳过坏文件继续用其余策略。"""
    bad = tmp_path / "configs/access"
    bad.mkdir(parents=True)
    (bad / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(AccessSnapshotError):
        load_snapshot(bad)


def test_readme_quickstart_includes_the_acl_step():
    """新克隆没有 configs/access/（被忽略），少了这一步 MCP 起不来。

    这不是故障而是默认拒绝，但快速开始必须写出来，否则读者会以为服务坏了。
    """
    quickstart = readme().split("## 本机快速开始", 1)[1].split("## ", 1)[0]
    assert "manage_access.py --sync" in quickstart


def test_readme_states_acl_must_be_mounted_and_private_is_unsupported():
    text = readme()
    assert "不带访问策略" in text
    assert "默认拒绝所有人" in text
    assert "不在镜像支持范围内" in text


def test_readme_says_usable_is_a_preflight_not_acceptance():
    """`usable` 只查四件事，不能被读成完整准入结论。"""
    assert "预检结论，不是验收结论" in readme()


# --- 运行时索引注册表：切到非公开目标后不能进发布内容 ---

REGISTRY_RELATIVE = "data/indexes/index-registry.json"


def test_runtime_registry_is_excluded_from_git_and_image():
    assert excluded_from_git(REGISTRY_RELATIVE), "运行时注册表仍会被提交"
    assert excluded_from_image(REGISTRY_RELATIVE), "运行时注册表仍会进镜像"


def test_registry_is_not_tracked_by_git():
    """被忽略还不够——已跟踪的文件不会因为新增忽略规则就自动退出。"""
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", REGISTRY_RELATIVE],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert tracked.returncode != 0, (
        "运行时注册表仍被 Git 跟踪；需要 git rm --cached 才能让它退出发布内容"
    )


def test_private_target_leaves_identifiers_in_the_registry(tmp_path):
    """先证明风险是真的：切到非公开目标后，注册表里确实会留下内部标识。

    注册表保持同一个文件、回退只把历史弹回 active 的位置，所以这些字段会一直留着。
    唯一的防线就是让这个文件不进入发布内容——上面那条测试守的就是它。
    """
    from wenjin.index_registry import IndexRegistryStore

    def target(target_id: str, manifest: str) -> IndexTarget:
        return IndexTarget(
            target_id=target_id, corpus_id="secret-project-1", library="secret-project",
            version="1", strategy="blocks-split", dense_collection="secret_dense",
            bm25_path="data/indexes/secret.sqlite", manifest_path=manifest,
            manifest_sha256="a" * 64, chunk_count=1, vector_dimension=1024,
            embedding_model="fake",
        )

    public = target("public-1", "corpora/public-1/source-manifest.json")
    private = target("secret-1", "corpora-private/secret-project-1/source-manifest.json")

    store = IndexRegistryStore(tmp_path / "index-registry.json")
    path = tmp_path / "index-registry.json"
    store.initialize(public, reason="bootstrap")

    store.switch(private, reason="临时切到内部语料")
    switched = path.read_text(encoding="utf-8")
    # 切换期间：完整目标信息（含内部 manifest 路径和 collection 名）就在 active 里。
    assert "corpora-private/secret-project-1" in switched
    assert "secret_dense" in switched

    store.rollback(reason="切回公开目标")
    rolled_back = path.read_text(encoding="utf-8")
    assert store.active().target_id == "public-1"
    # 回退把 history 弹回 active，所以内部 **路径** 不再留在注册表里；
    # 但 events 是审计流水，会一直保留内部 target_id。
    assert "corpora-private/secret-project-1" not in rolled_back
    assert "secret-1" in rolled_back, (
        "回退后 events 里仍留着内部 target_id——这正是注册表不能进发布内容的原因"
    )


# --- 历史评测文档必须保持其证据口径 ---
#
# README 只提供历史文档导航；这些合同锁定被迁出的结果说明，不要求把历史正文放回 README。


def readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def retrieval_experiments_doc() -> str:
    """历史评测事实归档在实验文档，README 只负责提供导航。"""
    return (ROOT / "docs/retrieval-experiments.md").read_text(encoding="utf-8")


def test_retrieval_experiments_doc_splits_the_evidence_coverage_by_basis():
    """64 个区间里只有 51 个是文本级；13 个页级不能说成"联合完整覆盖"。"""
    text = retrieval_experiments_doc()
    assert "64 个证据区间，**按判定依据拆开看**" in text
    assert "不是证据原文覆盖" in text
    assert "64 个证据区间都由 chunk 联合完整覆盖" not in text


def test_retrieval_experiments_doc_labels_the_retrieval_table_as_a_mixed_basis():
    """"全证据@5" 混了行级与 PDF 页级两种判定，必须就地说明。"""
    text = retrieval_experiments_doc()
    assert '"全证据@5" 是混合口径' in text
    assert "只能判" in text and "该页有 chunk" in text


def test_readme_describes_the_actual_ocr_decline_behaviour():
    """拒绝 OCR 时整份文件只归档——不能写成"文字层页仍入库"。"""
    text = readme()
    assert "拒绝 OCR 时整份文件只归档、不参与检索" in text
    assert "没有实现" in text
    assert "拒绝 OCR 时该文件仍按文本层入库" not in text


def test_readme_describes_the_download_path_correctly():
    """路径是 manifest 登记路径，不是磁盘上的 raw_data 相对路径。"""
    text = readme()
    assert "GET /files/{corpus_id}/{语料内登记路径}" in text
    assert "GET /files/{corpus_id}/{raw_data相对路径}" not in text


def test_readme_says_the_runtime_registry_is_local_state():
    text = readme()
    assert "index-registry.json" in text
    assert "manage_indexes.py init" in text


# --- 首次建库：无注册表、无索引的环境里必须能跑通 ---
#
# 只断言"README 里有 init 字样"是不够的：真正的失败模式是**循环依赖**——
# 建库入口读运行时注册表，而注册表初始化需要索引已建好并通过校验。


BUILD_ENTRY_POINTS = ("build_dense_index.py", "build_bm25_index.py")


def make_fresh_root(tmp_path: Path) -> Path:
    """一个没有注册表、没有索引的最小项目根：语料 + 已提交的目标配置。"""
    from wenjin.content_hashing import hash_content, manifest_content_fingerprint

    root = tmp_path / "fresh"
    raw = root / "raw_data/osg/3.6.5/include/osg"
    raw.mkdir(parents=True)
    (raw / "Node").write_text("# Node\nvoid addDrawable(Drawable* d);\n", encoding="utf-8")
    hashed = hash_content((raw / "Node").read_bytes(),
                          normalization="text-newline-lf-v1", encoding="utf-8")

    manifest_dir = root / "corpora/osg-3.6.5"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "source-manifest.json"
    manifest = {
        "schema_version": 3, "corpus_id": "osg-3.6.5", "library": "osg", "version": "3.6.5",
        "raw_data_subdir": "osg/3.6.5", "license": "OSGPL",
        "files": [{"path": "include/osg/Node", "raw_sha256": hashed.raw_sha256,
                   "canonical_sha256": hashed.canonical_sha256,
                   "normalization": "text-newline-lf-v1", "encoding": "utf-8",
                   "indexable": True, "source_url": "https://example.org/osg/Node",
                   "logical_document_id": "osg-3.6.5:include/osg/Node"}],
    }
    manifest["canonical_corpus_sha256"] = manifest_content_fingerprint(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    config_dir = root / "configs/indexes"
    config_dir.mkdir(parents=True)
    (config_dir / "osg-3.6.5-blocks-dualhash.json").write_text(json.dumps({
        "target_id": "osg-3.6.5-blocks-dualhash-003", "corpus_id": "osg-3.6.5",
        "library": "osg", "version": "3.6.5", "strategy": "blocks",
        "dense_collection": "osg_3_6_5_dense_blocks_dualhash",
        "bm25_path": "data/indexes/osg_3_6_5_bm25_blocks_dualhash.sqlite",
        "manifest_path": "corpora/osg-3.6.5/source-manifest.json",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "chunk_count": 1, "vector_dimension": 1024, "embedding_model": "fake",
    }), encoding="utf-8")
    return root


def test_fresh_root_has_no_registry_or_index(tmp_path):
    root = make_fresh_root(tmp_path)
    assert not (root / "data/indexes/index-registry.json").exists()
    assert not list((root / "data").glob("**/*.sqlite")) if (root / "data").exists() else True


@pytest.mark.parametrize("script", BUILD_ENTRY_POINTS)
def test_build_entry_point_runs_without_a_registry(tmp_path, script):
    """冷启动的实测：没有注册表时建库入口必须能解析出产物名。

    这就是原来的循环依赖：入口先 `IndexRegistryStore(...).active()`，
    而注册表要等索引进好并校验通过才能 init。
    """
    root = make_fresh_root(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script),
         "--root", str(root), "--dry-run"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr.strip().splitlines()[-3:]
    payload = json.loads(proc.stdout)
    assert payload["index_target_id"] == "osg-3.6.5-blocks-dualhash-003"
    assert payload["chunks"] == 1


def test_build_entry_points_do_not_read_the_runtime_registry():
    """把"不依赖注册表"写成静态不变量，避免以后又被加回来。"""
    for script in BUILD_ENTRY_POINTS:
        source = (ROOT / "scripts/operations" / script).read_text(encoding="utf-8")
        assert "IndexRegistryStore" not in source, f"{script} 又读回了运行时注册表"
        assert "index-registry.json" not in source, f"{script} 又写死了注册表路径"


def test_readme_orders_the_first_time_setup_correctly():
    """顺序必须是：建索引 → 完整校验 → 注册表 → 访问策略 → 查询。"""
    quickstart = readme().split("## 本机快速开始", 1)[1].split("## ", 1)[0]
    positions = [
        quickstart.index("build_bm25_index.py"),
        quickstart.index("manage_indexes.py validate"),
        quickstart.index("manage_indexes.py init"),
        quickstart.index("manage_access.py --sync"),
        quickstart.index("search_docs.py"),
    ]
    assert positions == sorted(positions), "快速开始的步骤顺序被改乱了"


# --- 首次建库的真实链路：实际写入 → 完整准入 → 初始化注册表 ---
#
# `--dry-run` 到不了这一层：新生成的 chunk 必须带准确的解析器标记，否则完整准入会判
# "有ocr_derived标记但缺少parser"，建库能过、注册表初始化过不去。


class StubEmbedder:
    """占位向量：只验证建库元数据与准入链路，不测检索效果。"""

    def get_text_embedding_batch(self, texts, show_progress=False):
        return [[0.0] * 8 for _ in texts]


def build_fresh_index(root: Path, *, strategy: str = "blocks"):
    """在临时根里真的建一份索引资产；返回 (target, qdrant_client)。"""
    from qdrant_client import QdrantClient

    from wenjin.indexing.build import build_candidate_index

    client = QdrantClient(":memory:")
    target = build_candidate_index(
        manifest_path=root / "corpora/osg-3.6.5/source-manifest.json",
        target_id="osg-3.6.5-blocks-fresh", strategy=strategy,
        dense_collection="osg_3_6_5_dense_blocks_fresh",
        bm25_path=root / "data/indexes/osg_3_6_5_bm25_blocks_fresh.sqlite",
        qdrant_client=client, embedder=StubEmbedder(), embedding_model="fake",
        project_root=root,
    )
    return target, client


def test_load_documents_marks_the_parser_and_logical_id(tmp_path, monkeypatch):
    """加载器必须把解析器和逻辑文档 ID 传下去。

    不传的话新索引写成 parser=NULL + ocr_derived=False，而完整准入把这种组合判为
    "来源信息不完整"——建库不报错、init 才失败。
    """
    import ingest

    root = make_fresh_root(tmp_path)
    monkeypatch.setenv("WENJIN_RAW_DATA_ROOT", str(root / "raw_data"))
    documents = ingest.load_documents(root / "corpora/osg-3.6.5/source-manifest.json")
    assert documents[0]["parser"] == "text-line-v1"
    assert documents[0]["logical_document_id"] == "osg-3.6.5:include/osg/Node"


def test_fresh_build_passes_admission_and_initializes_registry(tmp_path, monkeypatch):
    """真实建库 → 完整准入通过 → 初始化临时注册表。"""
    from wenjin.index_audit import admit_index
    from wenjin.index_registry import IndexRegistryStore

    root = make_fresh_root(tmp_path)
    monkeypatch.setenv("WENJIN_RAW_DATA_ROOT", str(root / "raw_data"))
    target, client = build_fresh_index(root)

    payloads = client.scroll(target.dense_collection, limit=10, with_payload=True)[0]
    assert {(point.payload["parser"], point.payload["ocr_derived"])
            for point in payloads} == {("text-line-v1", False)}

    report = admit_index(target, root=root, qdrant_client=client)
    assert report["status"] == "PASS", report["problems"]

    registry_path = root / "data/indexes/index-registry.json"
    assert not registry_path.exists()
    store = IndexRegistryStore(registry_path)
    store.initialize(target, reason="首次初始化")
    assert store.active().target_id == target.target_id


def test_fresh_build_keeps_chunk_ids_and_text_unchanged(tmp_path, monkeypatch):
    """加上解析器标记不能改动正文或 chunk ID：已有索引的兼容性靠这一点。"""
    import ingest
    from wenjin.ingestion.chunking import block_chunks

    root = make_fresh_root(tmp_path)
    monkeypatch.setenv("WENJIN_RAW_DATA_ROOT", str(root / "raw_data"))
    document = ingest.load_documents(root / "corpora/osg-3.6.5/source-manifest.json")[0]
    without_parser = {key: value for key, value in document.items() if key != "parser"}
    with_parser = block_chunks(document, max_chars=1600)[0]
    plain = block_chunks({**without_parser, "parser": None}, max_chars=1600)[0]
    assert with_parser.chunk_id == plain.chunk_id
    assert with_parser.text == plain.text
    assert with_parser.content_sha256 == plain.content_sha256


def test_bm25_script_writes_every_artifact_under_the_given_root(tmp_path):
    """非 dry-run：SQLite 和报告都必须落在选定的根里，仓库不被写入。

    原来报告写到固定 ROOT、且没有创建父目录，全新临时根里会 FileNotFoundError。
    """
    root = make_fresh_root(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_bm25_index.py"), "--root", str(root)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr.strip().splitlines()[-3:]
    assert (root / "data/indexes/osg_3_6_5_bm25_blocks_dualhash.sqlite").is_file()
    report_path = root / "artifacts/runs/bm25-index-blocks.json"
    assert report_path.is_file(), "报告没有写在临时根里"
    assert json.loads(report_path.read_text(encoding="utf-8"))["index"].startswith(str(root))


# --- 评测数字必须区分 validation 与冻结 test ---
#
# 方案是按 validation 的表现挑的，只引用它等于自己出题自己判卷。这里把"两套数字
# 必须并列出现、且指明该引用哪个"钉住，避免以后又退回只报 0.96。


def test_retrieval_experiments_doc_reports_both_splits_with_the_test_number_as_the_headline():
    text = retrieval_experiments_doc()
    assert "两套数字要分开读" in text
    assert "对外的预期性能应引用 test 的数字" in text
    assert "0.96" in text and "0.750" in text


def test_retrieval_experiments_doc_does_not_claim_the_validation_number_as_the_result():
    """validation 那行必须标出"偏乐观"，不能光秃秃地当结论。"""
    text = retrieval_experiments_doc()
    section = text.split("## 评测结果", 1)[1].split("### 冻结 test", 1)[0]
    assert "偏乐观" in section


def test_retrieval_experiments_doc_states_the_frozen_test_was_run_once_and_is_now_spent():
    text = retrieval_experiments_doc()
    assert "首次也是唯一一次运行" in text
    assert "不再作为未用过的考卷" in text


def test_retrieval_experiments_doc_records_the_pre_run_dataset_revision():
    """跑之前改过一列，必须写出来，并说明改在见到结果之前。"""
    text = retrieval_experiments_doc()
    assert "ogc-3dtiles" in text
    assert "改在见到任何结果之前" in text
