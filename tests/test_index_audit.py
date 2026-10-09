"""第三批：索引完整性与路径门禁的变异测试。

从一份**自洽**的合成资产出发，每次只篡改一处，对应检查必须把它拦下来。
重点覆盖"两侧索引同时填入相同错误 Hash"——只比较两侧是否一致是查不出来的，
必须回到 manifest 的登记值。
"""

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from wenjin import manifest as manifest_module
from wenjin.index_audit import audit_index
from wenjin.index_registry import IndexTarget, validate_target

CHUNK_TEXT = "class Node;\nvoid addDrawable();\n"
CHUNK_ID = "3.6.5:include/osg/Node:blocks:0000"
FILE_PATH = "include/osg/Node"
RAW_HASH = hashlib.sha256(CHUNK_TEXT.encode()).hexdigest()
CANONICAL_HASH = RAW_HASH
NORMALIZATION = "text-newline-lf-v1"
LOGICAL_ID = "osg-3.6.5:include/osg/Node"
CORPUS_DIR = "osg-3.6.5"


def build_assets(tmp_path: Path) -> dict:
    """造一份自洽的合成资产：语料 + manifest + BM25 + 假的 Qdrant。"""
    corpus_dir = tmp_path / "corpora" / CORPUS_DIR
    corpus_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = corpus_dir / "source-manifest.json"
    manifest = {
        "schema_version": 3,
        "corpus_id": CORPUS_DIR,
        "library": "osg",
        "version": "3.6.5",
        "raw_data_subdir": "osg/3.6.5",
        "license": "OSGPL",
        "files": [{
            "path": FILE_PATH,
            "raw_sha256": RAW_HASH,
            "canonical_sha256": CANONICAL_HASH,
            "normalization": NORMALIZATION,
            "logical_document_id": LOGICAL_ID,
            "encoding": "utf-8",
            "indexable": True,
        }],
    }
    manifest["canonical_corpus_sha256"] = manifest_module.canonical_fingerprint(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # 写出来源文件：行号范围校验要拿它的实际行数做上界。
    raw_file = tmp_path / "raw_data" / "osg" / "3.6.5" / FILE_PATH
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    # 语料来源的Hash按这组UTF-8字节计算；write_text在Windows会写入CRLF，
    # 因而这里显式write_bytes，避免夹具声明的raw_hash与实际来源不一致。
    raw_file.write_bytes(CHUNK_TEXT.encode("utf-8"))

    bm25_path = tmp_path / "candidate.sqlite"
    payload = {
        "chunk_id": CHUNK_ID,
        "source_path": FILE_PATH,
        "version": "3.6.5",
        "start_line": 1,
        "end_line": 2,
        "strategy": "blocks",
        "text": CHUNK_TEXT,
        "content_sha256": hashlib.sha256(CHUNK_TEXT.encode()).hexdigest(),
        "source_raw_sha256": RAW_HASH,
        "source_canonical_sha256": CANONICAL_HASH,
        "normalization": NORMALIZATION,
        "page_number": None,
        "logical_document_id": LOGICAL_ID,
    }
    write_bm25(bm25_path, payload)
    target = IndexTarget(
        target_id="osg-3.6.5-blocks-synth",
        corpus_id=CORPUS_DIR,
        library="osg",
        version="3.6.5",
        strategy="blocks",
        dense_collection="synth_collection",
        bm25_path="candidate.sqlite",
        manifest_path=f"corpora/{CORPUS_DIR}/source-manifest.json",
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        chunk_count=1,
        vector_dimension=3,
        embedding_model="test",
        canonical_corpus_sha256=manifest["canonical_corpus_sha256"],
        normalization_profile=NORMALIZATION,
    )
    return {
        "target": target,
        "manifest_path": manifest_path,
        "bm25_path": bm25_path,
        "payload": payload,
    }


def write_bm25(path: Path, payload: dict) -> None:
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as database:
        database.execute("""
            CREATE TABLE chunks(
                chunk_id, source_path, version, start_line, end_line, strategy,
                content_sha256, source_raw_sha256, source_canonical_sha256,
                normalization, page_number, logical_document_id, retrieval_text, raw_text
            )""")
        database.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (payload["chunk_id"], payload["source_path"], payload["version"],
             payload["start_line"], payload["end_line"], payload["strategy"],
             payload["content_sha256"], payload["source_raw_sha256"],
             payload["source_canonical_sha256"], payload["normalization"],
             payload["page_number"], payload["logical_document_id"], "", payload["text"]),
        )
        database.commit()


class FakeQdrant:
    """只实现审计用到的三件事：get_collection / count / scroll。

    接受单个payload或payload列表（后者用于"重复chunk ID"这类场景），
    points_count 跟随实际点数，否则点数校验会先失败、测不到重复检查。
    """

    def __init__(self, payloads, *, points: int | None = None) -> None:
        if isinstance(payloads, dict):
            payloads = [payloads]
        self.payloads = [dict(item) for item in payloads]
        self.points = points if points is not None else len(self.payloads)

    def get_collection(self, name):
        return SimpleNamespace(
            status=SimpleNamespace(value="green"),
            points_count=self.points,
            config=SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=3))),
        )

    def count(self, name, *, count_filter, exact):
        return SimpleNamespace(count=self.points)

    def scroll(self, name, *, limit, offset, with_payload, with_vectors):
        return ([SimpleNamespace(payload=dict(item)) for item in self.payloads], None)


def run_audit(assets: dict) -> dict:
    return audit_index(assets["target"], root=assets["root"], qdrant_client=FakeQdrant(assets["payload"]))


@pytest.fixture()
def assets(tmp_path, monkeypatch):
    built = build_assets(tmp_path)
    built["root"] = tmp_path
    return built


def test_synthetic_assets_pass_before_mutation(assets):
    """基线：未篡改时必须PASS，否则后面的变异测试没有意义。"""
    report = run_audit(assets)
    assert report["status"] == "PASS", report["problems"]
    assert report["manifest_records"] == 1


# --- 逐类篡改：每一处都必须被对应检查拦下 ---


def test_tampered_source_path_is_caught(assets):
    payload = {**assets["payload"], "source_path": "include/osg/Group"}
    report = audit_index(assets["target"], root=assets["root"],
                         qdrant_client=FakeQdrant(payload))
    assert report["problems"]["source_path_unmapped"]


def test_tampered_version_is_caught(assets):
    payload = {**assets["payload"], "version": "9.9.9"}
    report = audit_index(assets["target"], root=assets["root"],
                         qdrant_client=FakeQdrant(payload))
    assert report["problems"]["version_mismatch"]


def test_tampered_strategy_is_caught(assets):
    payload = {**assets["payload"], "strategy": "fixed"}
    report = audit_index(assets["target"], root=assets["root"],
                         qdrant_client=FakeQdrant(payload))
    assert report["problems"]["strategy_mismatch"]


def test_tampered_text_is_caught(assets):
    """正文被改但Hash未改时，Hash自检和原文切片核验都能发现。"""
    tampered = "class Node;\nvoid removed();\n"
    payload = {**assets["payload"], "text": tampered}  # 故意保留原content_sha256
    write_bm25(assets["bm25_path"], payload)
    report = audit_index(assets["target"], root=assets["root"],
                         qdrant_client=FakeQdrant(payload))
    assert report["problems"]["content_hash_mismatch"]


def test_tampered_source_hash_on_both_sides_is_caught(assets):
    """两侧索引同时填入相同的错误Hash——只比对两侧一致是查不出来的，
    必须回到manifest的登记值。
    """
    wrong = "a" * 64
    payload = {**assets["payload"], "source_raw_sha256": wrong}
    write_bm25(assets["bm25_path"], payload)  # BM25与Qdrant填同一个错误值
    report = audit_index(assets["target"], root=assets["root"],
                         qdrant_client=FakeQdrant(payload))
    assert report["problems"]["source_hash_mismatch"], "两侧填相同错误Hash未被拦截"
    assert "source_raw_sha256" in report["problems"]["source_hash_mismatch"][0]["detail"]


def test_tampered_declared_fingerprint_is_caught(assets):
    """只改manifest自己声明的指纹、不动记录，也必须被拒绝。

    做法：把声明值改成任意值，同时把目标配置的 manifest_sha256 更新为新字节的哈希
    （否则会先被manifest字节绑定拦下，测不到指纹这一层），再把目标的
    canonical_corpus_sha256 也设成那个伪造值——如果校验相信声明值，就会放行。
    """
    forged = "b" * 64
    data = json.loads(assets["manifest_path"].read_text(encoding="utf-8"))
    data["canonical_corpus_sha256"] = forged
    data["retrieval_content_sha256"] = forged
    assets["manifest_path"].write_text(json.dumps(data), encoding="utf-8")

    tampered_target = replace(
        assets["target"],
        canonical_corpus_sha256=forged,
        manifest_sha256=hashlib.sha256(assets["manifest_path"].read_bytes()).hexdigest(),
    )
    with pytest.raises(ValueError, match="按记录重算"):
        validate_target(tampered_target, root=assets["root"],
                        qdrant_client=FakeQdrant(assets["payload"]))


def test_declared_fingerprint_inconsistent_with_records_is_caught(assets):
    """声明值与按记录重算的结果不一致时，即使目标配置没被改也要拒绝。"""
    data = json.loads(assets["manifest_path"].read_text(encoding="utf-8"))
    data["retrieval_content_sha256"] = "c" * 64
    assets["manifest_path"].write_text(json.dumps(data), encoding="utf-8")

    updated = replace(
        assets["target"],
        manifest_sha256=hashlib.sha256(assets["manifest_path"].read_bytes()).hexdigest(),
    )
    # 目标配置里的指纹仍是按原记录算出的值，与声明值不一致，但重算能对上目标配置，
    # 所以这一层要专门检查"声明值 vs 重算值"。
    with pytest.raises(ValueError, match="声明"):
        validate_target(updated, root=assets["root"], qdrant_client=FakeQdrant(assets["payload"]))


def test_tampered_normalization_and_logical_id_are_caught(assets):
    payload = {**assets["payload"], "normalization": "binary-identity-v1",
               "logical_document_id": "other:doc"}
    report = audit_index(assets["target"], root=assets["root"],
                         qdrant_client=FakeQdrant(payload))
    assert report["problems"]["normalization_mismatch"]
    assert report["problems"]["logical_document_id_mismatch"]


def test_id_set_mismatch_is_caught(assets):
    payload = {**assets["payload"], "chunk_id": "different-id"}
    report = audit_index(assets["target"], root=assets["root"],
                         qdrant_client=FakeQdrant(payload))
    assert report["problems"]["id_sets_differ"]


# --- 路径门禁 ---


def test_manifest_rejects_raw_data_subdir_escape(tmp_path):
    """subdir 本身越界时必须先被拒绝，否则"每个文件都在raw_root内"毫无意义。"""
    manifest = {"raw_data_subdir": "../../etc", "files": []}
    with pytest.raises(manifest_module.ManifestError, match="越界或非法"):
        manifest_module.trusted_raw_root(manifest, tmp_path / "raw_data")


def test_manifest_rejects_absolute_file_path(tmp_path):
    manifest = {"files": [{"path": "/etc/passwd", "raw_sha256": "0" * 64}]}
    with pytest.raises(manifest_module.ManifestError, match="越界或非法"):
        manifest_module.file_records(manifest)


def test_manifest_rejects_missing_or_bad_hash(tmp_path):
    for entry in ({"path": "a.md"},
                  {"path": "a.md", "raw_sha256": ""},
                  {"path": "a.md", "raw_sha256": "zz"}):
        with pytest.raises(manifest_module.ManifestError):
            manifest_module.file_records({"files": [entry]})


def test_manifest_rejects_unsafe_identifiers(tmp_path):
    manifest = {"library": "../evil", "version": "1.0", "files": []}
    with pytest.raises(Exception):
        manifest_module.validate_identity(manifest, manifest_path=tmp_path / "x" / "m.json")


# --- 范围语义必须并存，不能被改动 ---


def test_scope_semantics_preserved_for_both_generations():
    """legacy OSG 用 indexable；新语料用 retrieval_eligible。两者各自保持原样。"""
    legacy = {"files": [{"path": "a", "indexable": True}, {"path": "b", "indexable": False}]}
    assert manifest_module.scope_files(legacy) == ["a"]
    assert manifest_module.uses_retrieval_scope(legacy) is False

    modern = {"files": [{"path": "a", "retrieval_eligible": True, "indexable": False},
                        {"path": "b", "retrieval_eligible": False, "indexable": False}]}
    assert manifest_module.scope_files(modern) == ["a"]
    assert manifest_module.uses_retrieval_scope(modern) is True


def test_fingerprint_algorithm_reproduces_real_corpora():
    """重算算法必须与既有实现一致，否则已验收的指纹会全部失配。"""
    root = Path(__file__).resolve().parents[1]
    for manifest_path in sorted((root / "corpora").glob("*/source-manifest.json")):
        manifest = manifest_module.load(manifest_path)
        recomputed = manifest_module.retrieval_fingerprint(manifest)
        declared = (manifest.get("retrieval_content_sha256")
                    or manifest_module.canonical_fingerprint(manifest))
        assert recomputed == declared, manifest_path


# --- 路径门禁要覆盖真实加载入口，而不只是底层函数 ---


def test_load_retrieval_documents_rejects_subdir_escape(tmp_path):
    """`load_retrieval_documents` 必须先确认 raw_data_subdir 在可信根内。

    修复前它直接 `(raw_data_root / subdir).resolve()`，subdir 写成 `../..` 就能
    把"每个文件都在 raw_root 内"这个检查架空。
    """
    from wenjin.ingestion.multiformat import load_retrieval_documents

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET\n", encoding="utf-8")
    raw_root = tmp_path / "raw_data"
    raw_root.mkdir()

    corpus_dir = tmp_path / "corpora" / "evil-1.0"
    corpus_dir.mkdir(parents=True)
    manifest_path = corpus_dir / "source-manifest.json"
    manifest_path.write_text(json.dumps({
        "corpus_id": "evil-1.0", "library": "evil", "version": "1.0",
        "raw_data_subdir": "../outside",
        "files": [{"path": "secret.txt", "raw_sha256": "0" * 64,
                   "canonical_sha256": "0" * 64, "encoding": "utf-8",
                   "normalization": NORMALIZATION, "retrieval_eligible": True}],
    }), encoding="utf-8")

    with pytest.raises(manifest_module.ManifestError, match="越界或非法"):
        load_retrieval_documents(manifest_path, raw_root)


def test_load_retrieval_documents_rejects_file_path_escape(tmp_path):
    """subdir 合法但单条文件路径越界时也要拒绝。"""
    from wenjin.ingestion.multiformat import load_retrieval_documents

    raw_root = tmp_path / "raw_data" / "lib" / "1.0"
    raw_root.mkdir(parents=True)
    (tmp_path / "raw_data" / "secret.txt").write_text("SECRET\n", encoding="utf-8")

    corpus_dir = tmp_path / "corpora" / "lib-1.0"
    corpus_dir.mkdir(parents=True)
    manifest_path = corpus_dir / "source-manifest.json"
    manifest_path.write_text(json.dumps({
        "corpus_id": "lib-1.0", "library": "lib", "version": "1.0",
        "raw_data_subdir": "lib/1.0",
        "files": [{"path": "../secret.txt", "raw_sha256": "0" * 64,
                   "canonical_sha256": "0" * 64, "encoding": "utf-8",
                   "normalization": NORMALIZATION, "retrieval_eligible": True}],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="语料路径无效"):
        load_retrieval_documents(manifest_path, tmp_path / "raw_data")


# --- 第三批复审（第二轮）：5 处漏检的回归测试 ---
#
# 这些测试在修复前应当失败。每条对应复审编号。


def make_flexible(tmp_path: Path, *, indexable: bool = True, duplicate_manifest_path: bool = False,
                  duplicate_chunk: bool = False, subdir: str = "osg/3.6.5",
                  retrieval_eligible: bool | None = None) -> dict:
    """可调的合成资产：用于复现"登记但不可检索"、重复键、subdir 逃逸等情形。"""
    built = build_assets(tmp_path)
    data = json.loads(built["manifest_path"].read_text(encoding="utf-8"))
    entry = data["files"][0]
    entry["indexable"] = indexable
    if retrieval_eligible is not None:
        entry["retrieval_eligible"] = retrieval_eligible
    if duplicate_manifest_path:
        data["files"].append(dict(entry))
    data["raw_data_subdir"] = subdir
    data["canonical_corpus_sha256"] = manifest_module.canonical_fingerprint(data)
    data["retrieval_content_sha256"] = manifest_module.retrieval_fingerprint(data)
    built["manifest_path"].write_text(json.dumps(data), encoding="utf-8")

    if duplicate_chunk:
        with closing(sqlite3.connect(built["bm25_path"])) as database:
            database.execute(
                "INSERT INTO chunks SELECT * FROM chunks WHERE chunk_id = ?", (CHUNK_ID,)
            )
            database.commit()

    built["root"] = tmp_path
    count = 2 if duplicate_chunk else 1
    built["target"] = replace(
        built["target"],
        manifest_sha256=hashlib.sha256(built["manifest_path"].read_bytes()).hexdigest(),
        canonical_corpus_sha256=data["canonical_corpus_sha256"],
        chunk_count=count,
    )
    built["count"] = count
    return built


def audit_with(assets: dict, *, payload: dict | None = None,
               payloads: list[dict] | None = None, count: int | None = None) -> dict:
    items = payloads or [payload or assets["payload"]]
    if count and count > len(items):
        items = items * count
    client = FakeQdrant(items, points=count or len(items))
    return audit_index(assets["target"], root=assets["root"], qdrant_client=client)


def set_bm25(assets: dict, column: str, value) -> None:
    with closing(sqlite3.connect(assets["bm25_path"])) as database:
        database.execute(f"UPDATE chunks SET {column} = ?", (value,))
        database.commit()


def get_bm25(assets: dict, column: str):
    with closing(sqlite3.connect(assets["bm25_path"])) as database:
        return database.execute(f"SELECT {column} FROM chunks").fetchone()[0]


def test_review2_bm25_only_tampering_is_caught(assets):
    """只改 BM25 一侧的字段，必须被发现——不能只校验 Qdrant payload。

    逐条改完再改回原值；不重建文件（Windows 上打开中的 sqlite 无法删除重写）。
    """
    for column, value, expected in (
        ("source_path", "include/osg/Group", "source_path_unmapped"),
        ("source_raw_sha256", "a" * 64, "source_hash_mismatch"),
        ("canonical_sha256_placeholder", None, None),  # 占位，见下方单独用例
        ("content_sha256", "b" * 64, "content_hash_mismatch"),
        ("start_line", 99, "position_mismatch"),
        ("logical_document_id", "other", "logical_document_id_mismatch"),
        ("normalization", "binary-identity-v1", "normalization_mismatch"),
    ):
        if column.endswith("_placeholder"):
            continue
        original = get_bm25(assets, column)
        set_bm25(assets, column, value)
        try:
            try:
                report = audit_with(assets)
            except ValueError:
                # 有些字段（如normalization）在更早的三资产校验就被拦下，
                # 同样是"被拒绝"，只是不落在逐chunk的problem分类里。
                caught = True
            else:
                caught = bool(report["problems"][expected])
            assert caught, f"只改BM25的{column}未被任何检查拦截"
        finally:
            set_bm25(assets, column, original)
        # 还原后必须回到PASS，证明上一条测的是该字段而不是别的原因
        assert audit_with(assets)["status"] == "PASS", f"{column} 还原后仍不通过"


def test_review2_qdrant_only_tampering_is_caught(assets):
    """只改 Qdrant 一侧时，行号/页码同样要被抓到。"""
    for field, value, expected in (
        ("start_line", 99, "position_mismatch"),
        ("end_line", 99, "position_mismatch"),
        ("page_number", 7, "position_mismatch"),
    ):
        payload = {**assets["payload"], field: value}
        report = audit_with(assets, payload=payload)
        assert report["problems"][expected], f"只改Qdrant的{field}未被拦截"


def test_review2_both_sides_tampered_position_is_caught(assets):
    """两侧同时改行号：一致但都不是登记位置，仍应被抓到。"""
    payload = {**assets["payload"], "start_line": 99, "end_line": 100}
    with closing(sqlite3.connect(assets["bm25_path"])) as database:
        database.execute("UPDATE chunks SET start_line = 99, end_line = 100")
        database.commit()
    report = audit_with(assets, payload=payload)
    assert report["problems"]["position_mismatch"], "两侧一致但越界的行号未被拦截"


def test_review2_non_retrievable_file_must_not_be_indexed(tmp_path):
    """登记但"只归档、不检索"的文件，不允许出现在索引里。"""
    flexible = make_flexible(tmp_path, indexable=False, retrieval_eligible=False)
    report = audit_with(flexible)
    assert report["problems"]["outside_retrieval_scope"], "不可检索的文件仍留在索引中"


def test_review2_legacy_scope_also_enforced(tmp_path):
    """legacy 语义同上：indexable=False 的文件不允许进索引。"""
    flexible = make_flexible(tmp_path, indexable=False, retrieval_eligible=None)
    report = audit_with(flexible)
    assert report["problems"]["outside_retrieval_scope"]


def test_review2_duplicate_chunk_ids_are_rejected(tmp_path):
    """重复 chunk ID 不能靠字典覆盖掉：两侧都重复，集合比较看不出。"""
    flexible = make_flexible(tmp_path, duplicate_chunk=True)
    report = audit_with(flexible, payloads=[flexible["payload"], flexible["payload"]],
                        count=2)
    assert report["problems"]["duplicate_chunk_ids"], "重复chunk ID未被拦截"


def test_review2_duplicate_manifest_paths_are_rejected(tmp_path):
    """manifest 里重复登记同一路径必须拒绝：来源映射不再唯一。

    这条在 manifest 结构校验阶段就抛异常（fail closed），不会走到逐 chunk 比较。
    """
    flexible = make_flexible(tmp_path, duplicate_manifest_path=True)
    with pytest.raises(manifest_module.ManifestError, match="重复登记"):
        audit_with(flexible)


def test_review2_audit_rejects_raw_data_subdir_escape(tmp_path):
    """完整审计必须校验 subdir：导入拒绝的配置，验收也不能接受。

    在结构校验阶段直接拒绝（抛异常），而不是产出一份 FAIL 报告——
    坏配置根本不该进入逐条比较。
    """
    flexible = make_flexible(tmp_path, subdir="../outside")
    with pytest.raises(manifest_module.ManifestError, match="越界或非法"):
        audit_with(flexible)


def test_review2_file_records_rejects_drive_letter_path():
    """`C:/outside/a.txt` 在 Windows 上是绝对路径，不能只看前导斜杠。"""
    for bad in ("C:/outside/a.txt", r"C:\outside\a.txt", "/abs/x",
                r"\\server\share\x", "C:a.txt"):
        with pytest.raises(manifest_module.ManifestError):
            manifest_module.file_records({"files": [{"path": bad, "raw_sha256": "0" * 64}]})


# --- 统一准入：各入口必须用同一份判定 ---


def test_admission_raises_on_bad_asset(assets):
    """统一准入入口在审计失败时必须抛异常，而不是返回一份报告让调用方自行判断。"""
    from wenjin.index_audit import IndexAdmissionError, admit_index

    payload = {**assets["payload"], "source_raw_sha256": "a" * 64}
    with pytest.raises(IndexAdmissionError, match="准入审计"):
        admit_index(assets["target"], root=assets["root"],
                    qdrant_client=FakeQdrant(payload))


def test_admission_passes_on_clean_asset(assets):
    """对照组：干净资产必须通过，证明上一条测的是准入而不是函数本身总抛异常。"""
    from wenjin.index_audit import admit_index

    report = admit_index(assets["target"], root=assets["root"],
                         qdrant_client=FakeQdrant(assets["payload"]))
    assert report["status"] == "PASS"


def test_every_entry_point_uses_full_admission():
    """启动、路由、切换、回退、候选验收都必须调用统一准入。

    只做静态检查：确保这些位置没有退回各自的基础校验。逐条行为已由上面的
    audit_index/admit_index 测试覆盖。
    """
    root = Path(__file__).resolve().parents[1]
    expectations = {
        "src/wenjin/mcp/runtime.py": "admit_index",
        "src/wenjin/retrieval/router.py": "admit_index",
        "scripts/operations/manage_indexes.py": "admit_index",
        "scripts/operations/verify_candidate_indexes.py": "audit_index",
    }
    for relative, required in expectations.items():
        text = (root / relative).read_text(encoding="utf-8")
        assert required in text, f"{relative} 未使用统一准入 {required}"

    # 启动、路由与切换/回退不能再出现"只做基础校验"的调用。
    for relative in ("src/wenjin/mcp/runtime.py", "src/wenjin/retrieval/router.py"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "validate_target(" not in text, f"{relative} 仍直接调用基础校验"


# --- 第三批复审（第三轮）：来源一致性与PDF页码 ---


def make_two_file_corpus(tmp_path: Path, *, same_content: bool = True) -> dict:
    """登记两个文件；内容相同则Hash相同，用来测"Hash相同但来源不同"。"""
    raw_dir = tmp_path / "raw_data" / "osg" / "3.6.5" / "include" / "osg"
    raw_dir.mkdir(parents=True, exist_ok=True)
    # manifest中的raw_hash基于UTF-8 LF字节；Windows文本模式会把LF写成CRLF。
    (raw_dir / "Node").write_bytes(CHUNK_TEXT.encode("utf-8"))
    (raw_dir / "OtherNode").write_bytes(CHUNK_TEXT.encode("utf-8"))

    corpus_dir = tmp_path / "corpora" / CORPUS_DIR
    corpus_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = corpus_dir / "source-manifest.json"
    manifest = {
        "schema_version": 3, "corpus_id": CORPUS_DIR, "library": "osg", "version": "3.6.5",
        "raw_data_subdir": "osg/3.6.5", "license": "OSGPL",
        "files": [
            {"path": "include/osg/Node", "raw_sha256": RAW_HASH, "canonical_sha256": RAW_HASH,
             "normalization": NORMALIZATION, "encoding": "utf-8", "indexable": True},
            {"path": "include/osg/OtherNode", "raw_sha256": RAW_HASH,
             "canonical_sha256": RAW_HASH, "normalization": NORMALIZATION,
             "encoding": "utf-8", "indexable": True},
        ],
    }
    manifest["canonical_corpus_sha256"] = manifest_module.canonical_fingerprint(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # BM25 指向 OtherNode，Qdrant 指向 Node —— 两边内容与Hash都相同
    bm25_path = tmp_path / "two.sqlite"
    assert same_content
    payload_bm25 = {
        "chunk_id": CHUNK_ID, "source_path": "include/osg/OtherNode", "version": "3.6.5",
        "start_line": 1, "end_line": 2, "strategy": "blocks", "text": CHUNK_TEXT,
        "content_sha256": hashlib.sha256(CHUNK_TEXT.encode()).hexdigest(),
        "source_raw_sha256": RAW_HASH, "source_canonical_sha256": RAW_HASH,
        "normalization": NORMALIZATION, "page_number": None, "logical_document_id": None,
    }
    write_bm25(bm25_path, payload_bm25)
    qdrant_payload = {**payload_bm25, "source_path": "include/osg/Node"}

    target = IndexTarget(
        target_id="osg-3.6.5-blocks-two", corpus_id=CORPUS_DIR, library="osg",
        version="3.6.5", strategy="blocks", dense_collection="two_collection",
        bm25_path="two.sqlite", manifest_path=f"corpora/{CORPUS_DIR}/source-manifest.json",
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        chunk_count=1, vector_dimension=3, embedding_model="test",
        canonical_corpus_sha256=manifest["canonical_corpus_sha256"],
        normalization_profile=NORMALIZATION,
    )
    return {"target": target, "root": tmp_path, "manifest_path": manifest_path,
            "bm25_path": bm25_path, "payload": qdrant_payload,
            "bm25_payload": payload_bm25, "count": 1}


def test_review3_sides_pointing_to_different_files_is_caught(tmp_path):
    """两个文件内容相同（Hash相同），但两侧指向不同来源——必须被拦下。

    Hash 只能证明内容相同，不能证明来源相同；放过去会让检索结果引用到
    另一个文件和另一条来源URL。
    """
    assets = make_two_file_corpus(tmp_path)
    report = audit_with(assets)
    assert report["problems"]["source_path_mismatch"], "两侧指向不同来源未被拦截"


def test_review3_same_file_two_path_spellings_passes():
    """对照：两种路径写法指向**同一条**登记记录时必须通过。

    新语料带`raw_data/<subdir>/`前缀，OSG是裸路径；两者都映射到同一记录，
    不该被判成"来源不同"。
    """
    from wenjin.serving import CorpusSources

    root = Path(__file__).resolve().parents[1]
    sources = CorpusSources.from_manifest(
        root / "corpora/osg-3.6.5/source-manifest.json",
        raw_root=root / "raw_data", expected_corpus_id="osg-3.6.5",
    )
    bare = sources.match_chunk_path("include/osg/Node").relative
    prefixed = sources.match_chunk_path("raw_data/osg/3.6.5/include/osg/Node").relative
    assert bare == prefixed == "include/osg/Node"


def make_pdf_corpus(tmp_path: Path, *, pages: int = 1) -> dict:
    """一页（或多页）PDF来源；chunk 页码由调用方设置。"""
    import pymupdf

    corpus_dir = tmp_path / "corpora" / "ogc-3dtiles-1.1"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = tmp_path / "raw_data" / "ogc-3dtiles" / "1.1"
    raw_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = raw_dir / "spec.pdf"
    document = pymupdf.open()
    for _ in range(pages):
        page = document.new_page()
        # 文本层页审计现在会逐字重放页正文；夹具应提供与chunk一致的实际PDF文字。
        page.insert_text((72, 72), "page text")
    document.save(pdf_path)
    document.close()
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

    manifest_path = corpus_dir / "source-manifest.json"
    manifest = {
        "schema_version": 3, "corpus_id": "ogc-3dtiles-1.1", "library": "ogc-3dtiles",
        "version": "1.1", "raw_data_subdir": "ogc-3dtiles/1.1", "license": "OGC",
        "files": [{"path": "spec.pdf", "raw_sha256": digest, "canonical_sha256": digest,
                   "normalization": "binary-identity-v1", "encoding": None,
                   "logical_document_id": "ogc-3dtiles-1.1:spec.pdf",
                   "parser": "pymupdf-page-v1", "retrieval_eligible": True,
                   "indexable": False}],
    }
    manifest["canonical_corpus_sha256"] = manifest_module.canonical_fingerprint(manifest)
    manifest["retrieval_content_sha256"] = manifest_module.retrieval_fingerprint(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    text = "page text"
    payload = {
        "chunk_id": "1.1:spec.pdf:page-1:blocks:0000", "source_path": "raw_data/ogc-3dtiles/1.1/spec.pdf",
        "version": "1.1", "start_line": 1, "end_line": 1, "strategy": "blocks",
        "text": text, "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_raw_sha256": digest, "source_canonical_sha256": digest,
        "normalization": "binary-identity-v1", "page_number": 1,
        "logical_document_id": "ogc-3dtiles-1.1:spec.pdf",
    }
    bm25_path = tmp_path / "pdf.sqlite"
    write_bm25(bm25_path, payload)
    target = IndexTarget(
        target_id="ogc-3dtiles-1.1-blocks-pdf", corpus_id="ogc-3dtiles-1.1",
        library="ogc-3dtiles", version="1.1", strategy="blocks",
        dense_collection="pdf_collection", bm25_path="pdf.sqlite",
        manifest_path="corpora/ogc-3dtiles-1.1/source-manifest.json",
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        chunk_count=1, vector_dimension=3, embedding_model="test",
        canonical_corpus_sha256=manifest["retrieval_content_sha256"],
        normalization_profile="binary-identity-v1",
    )
    return {"target": target, "root": tmp_path, "manifest_path": manifest_path,
            "bm25_path": bm25_path, "payload": payload, "count": 1}


def test_review3_valid_page_numbers_pass(tmp_path):
    """对照：合法页码（首页、末页）必须通过。"""
    assets = make_pdf_corpus(tmp_path, pages=3)
    for page in (1, 2, 3):
        set_bm25(assets, "page_number", page)
        report = audit_with(assets, payload={**assets["payload"], "page_number": page})
        assert report["status"] == "PASS", (page, report["problems"])


def test_review3_both_sides_missing_page_number_is_caught(tmp_path):
    """两侧同时缺页码：只比"列存在"和"两侧一致"都会漏过。"""
    assets = make_pdf_corpus(tmp_path, pages=1)
    set_bm25(assets, "page_number", None)
    report = audit_with(assets, payload={**assets["payload"], "page_number": None})
    assert report["problems"]["position_mismatch"], "两侧同时缺页码未被拦截"


def test_review3_both_sides_out_of_range_page_is_caught(tmp_path):
    """两侧同时写成越界页码（PDF只有1页却写999）必须被拦下。"""
    assets = make_pdf_corpus(tmp_path, pages=1)
    set_bm25(assets, "page_number", 999)
    report = audit_with(assets, payload={**assets["payload"], "page_number": 999})
    assert report["problems"]["position_mismatch"], "两侧同时越界页码未被拦截"


# --- 第四批：OCR 来源标记必须在索引层可核验 ---
#
# manifest 里的 parser 只说明"这份文件打算怎么解析"，不能证明索引里的正文真的
# 来自那里。只有把标记写进 chunk，证据才能在引用时说明"这段是 OCR 识别的"。


def write_bm25_with_parser(path: Path, payload: dict) -> None:
    """带 parser/ocr_derived 两列的 BM25（新 schema）。"""
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as database:
        database.execute("""
            CREATE TABLE chunks(
                chunk_id, source_path, version, start_line, end_line, strategy,
                content_sha256, source_raw_sha256, source_canonical_sha256,
                normalization, page_number, logical_document_id,
                parser, ocr_derived, retrieval_text, raw_text
            )""")
        database.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (payload["chunk_id"], payload["source_path"], payload["version"],
             payload["start_line"], payload["end_line"], payload["strategy"],
             payload["content_sha256"], payload["source_raw_sha256"],
             payload["source_canonical_sha256"], payload["normalization"],
             payload["page_number"], payload["logical_document_id"],
             payload.get("parser"), int(bool(payload.get("ocr_derived"))),
             "", payload["text"]),
        )
        database.commit()


def make_ocr_corpus(tmp_path: Path, *, declared: str, unit_parser: str,
                    marker: bool | None = None, with_parser_column: bool = True,
                    page_number: int = 1, plan_scan_pages: list[int] | None = None) -> dict:
    """一份单页 PDF 语料；文件级 parser 与 chunk 上的页级 parser 可分别指定。

    `plan_scan_pages` 给出 manifest 记录的逐页解析计划（哪些页是扫描页）。
    混合件必须带这份计划，否则审计无从判断某一页该由哪个解析器产出。
    """
    import pymupdf

    corpus_dir = tmp_path / "corpora" / "scan-1.0"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = tmp_path / "raw_data" / "scan" / "1.0"
    raw_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = raw_dir / "doc.pdf"
    document = pymupdf.open()
    for _ in range(max(page_number, 1)):
        page = document.new_page()
        # OCR页正文仍只作为识别结果标记；文本层页则需能与实际PDF正文逐字对照。
        page.insert_text((72, 72), "ocr body text")
    document.save(pdf_path)
    document.close()
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

    provenance: dict = {}
    if plan_scan_pages is not None:
        provenance["parse_report"] = {
            "pages": max(page_number, 1), "text_pages": max(page_number, 1) - len(plan_scan_pages),
            "scan_pages": list(plan_scan_pages), "no_text_pages": [],
            "thresholds": {"scan_image_coverage": 0.5},
        }
    manifest_path = corpus_dir / "source-manifest.json"
    manifest = {
        "schema_version": 3, "corpus_id": "scan-1.0", "library": "scan",
        "version": "1.0", "raw_data_subdir": "scan/1.0", "license": "unknown",
        "files": [{"path": "doc.pdf", "raw_sha256": digest, "canonical_sha256": digest,
                   "normalization": "binary-identity-v1", "encoding": None,
                   "logical_document_id": "scan-1.0:doc.pdf", "parser": declared,
                   "retrieval_eligible": True, "indexable": False,
                   "provenance": provenance}],
    }
    manifest["canonical_corpus_sha256"] = manifest_module.canonical_fingerprint(manifest)
    manifest["retrieval_content_sha256"] = manifest_module.retrieval_fingerprint(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    text = "ocr body text"
    derived = unit_parser == "mineru-ocr-page-v1"
    payload = {
        "chunk_id": f"1.0:doc.pdf:page-{page_number}:blocks:0000",
        "source_path": "raw_data/scan/1.0/doc.pdf", "version": "1.0",
        "start_line": 1, "end_line": 1, "strategy": "blocks", "text": text,
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_raw_sha256": digest, "source_canonical_sha256": digest,
        "normalization": "binary-identity-v1", "page_number": page_number,
        "logical_document_id": "scan-1.0:doc.pdf", "parser": unit_parser,
        "ocr_derived": derived if marker is None else marker,
    }
    bm25_path = tmp_path / "ocr.sqlite"
    if with_parser_column:
        write_bm25_with_parser(bm25_path, payload)
    else:
        write_bm25(bm25_path, payload)
        # 真正的旧索引两侧都没有这两个字段；只去掉 BM25 一列会变成"两侧不一致"，
        # 那是另一个缺陷，由 test_review4_parser_present_on_only_one_side_is_caught 覆盖。
        payload = {key: value for key, value in payload.items()
                   if key not in {"parser", "ocr_derived"}}
    target = IndexTarget(
        target_id="scan-1.0-blocks-ocr", corpus_id="scan-1.0", library="scan",
        version="1.0", strategy="blocks", dense_collection="ocr_collection",
        bm25_path="ocr.sqlite", manifest_path="corpora/scan-1.0/source-manifest.json",
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        chunk_count=1, vector_dimension=3, embedding_model="test",
        canonical_corpus_sha256=manifest["retrieval_content_sha256"],
        normalization_profile="binary-identity-v1",
    )
    return {"target": target, "root": tmp_path, "manifest_path": manifest_path,
            "bm25_path": bm25_path, "payload": payload, "count": 1}


def test_review4_ocr_marked_index_passes(tmp_path):
    """基线：manifest 与索引两侧都如实标注 OCR 时必须通过。"""
    assets = make_ocr_corpus(tmp_path, declared="mineru-ocr-page-v1",
                             unit_parser="mineru-ocr-page-v1")
    report = audit_with(assets)
    assert report["status"] == "PASS", report["problems"]
    assert report["parser_marker"] == {
        "column_present": True,
        "ocr_sources_in_manifest": ["doc.pdf"],
        "ocr_source_chunks": 1,
        "ocr_marker_verified_chunks": 1,
        "verifiable": True,
    }


def test_review4_missing_parser_column_for_ocr_source_is_caught(tmp_path):
    """manifest 说这份是 OCR 来源，索引却没有 parser 列——OCR 标记无从核验。"""
    assets = make_ocr_corpus(tmp_path, declared="mineru-ocr-page-v1",
                             unit_parser="mineru-ocr-page-v1",
                             with_parser_column=False)
    report = audit_with(assets)
    assert report["problems"]["legacy_schema_mismatch"]
    assert report["status"] == "FAIL"
    assert report["parser_marker"]["verifiable"] is False


def test_review4_unit_parser_outside_declared_set_is_caught(tmp_path):
    """文件级声明文本层解析，chunk 却标成 OCR——两侧说法冲突。"""
    assets = make_ocr_corpus(tmp_path, declared="pymupdf-page-v1",
                             unit_parser="mineru-ocr-page-v1")
    report = audit_with(assets)
    assert report["problems"]["parser_mismatch"]


def test_review4_ocr_flag_inconsistent_with_parser_is_caught(tmp_path):
    """`ocr_derived` 不能由写入方自己声明，必须与 parser 推导结果一致。"""
    assets = make_ocr_corpus(tmp_path, declared="mineru-ocr-page-v1",
                             unit_parser="mineru-ocr-page-v1", marker=False)
    report = audit_with(assets)
    assert report["problems"]["ocr_marker_mismatch"]


def test_review4_text_layer_index_without_column_is_not_a_failure(tmp_path):
    """对照：旧索引（两侧都没有 parser 字段）不因此失败。

    文本层语料没有"识别文本冒充原文"的风险，manifest 也用内容 Hash 把 chunk 绑定到
    来源。覆盖缺口由 parser_marker 单独报告，不混进"核验通过"。
    """
    assets = make_ocr_corpus(tmp_path, declared="pymupdf-page-v1",
                             unit_parser="pymupdf-page-v1", with_parser_column=False)
    report = audit_with(assets)
    assert report["status"] == "PASS", report["problems"]
    assert report["parser_marker"] == {
        "column_present": False,
        "ocr_sources_in_manifest": [],
        "ocr_source_chunks": 0,
        "ocr_marker_verified_chunks": 0,
        "verifiable": True,
    }


def test_review4_hybrid_file_follows_the_recorded_per_page_plan(tmp_path):
    """混合件按 manifest 的逐页计划核对：扫描页必须标 OCR，文本页必须标文本层。

    "允许集合里有这个 parser"是不够的——集合同时含文本层和 OCR，
    那样把扫描页标成文本层解析器也会通过，识别文本就洗掉了 OCR 身份。
    """
    cases = [
        (1, "mineru-ocr-page-v1", [1]),   # 计划说第1页是扫描页 -> 必须标 OCR
        (1, "pymupdf-page-v1", []),       # 空扫描页列表表示第1页走文本层
    ]
    for page, unit_parser, plan in cases:
        directory = tmp_path / f"p{page}-{unit_parser}"
        directory.mkdir()
        assets = make_ocr_corpus(directory, declared="mixed-text-ocr-page-v1",
                                 unit_parser=unit_parser, page_number=page,
                                 plan_scan_pages=plan)
        report = audit_with(assets)
        assert report["status"] == "PASS", (page, unit_parser, report["problems"])


def test_review4_scan_page_labelled_as_text_layer_is_caught(tmp_path):
    """混合件里扫描页被标成文本层解析器：识别文本会以原文的样子进入证据。"""
    assets = make_ocr_corpus(tmp_path, declared="mixed-text-ocr-page-v1",
                             unit_parser="pymupdf-page-v1", page_number=1,
                             plan_scan_pages=[1])
    report = audit_with(assets)
    assert report["status"] == "FAIL"
    assert report["problems"]["parser_mismatch"], report["problems"]
    detail = " ".join(item["detail"] for item in report["problems"]["parser_mismatch"])
    assert "逐页计划不符" in detail


def test_review4_text_page_labelled_as_ocr_is_caught(tmp_path):
    """反向：计划说这页不是扫描页，却标成 OCR——同样与记录的计划冲突。"""
    assets = make_ocr_corpus(tmp_path, declared="mixed-text-ocr-page-v1",
                             unit_parser="mineru-ocr-page-v1", page_number=2,
                             plan_scan_pages=[1])
    report = audit_with(assets)
    assert report["problems"]["parser_mismatch"], report["problems"]


def test_review4_hybrid_without_per_page_plan_is_caught(tmp_path):
    """混合件却没有记录逐页计划：判定依据缺失，不能当作核验通过。"""
    assets = make_ocr_corpus(tmp_path, declared="mixed-text-ocr-page-v1",
                             unit_parser="mineru-ocr-page-v1", page_number=1)
    report = audit_with(assets)
    assert report["status"] == "FAIL"
    assert report["problems"]["parser_mismatch"], report["problems"]
    detail = " ".join(item["detail"] for item in report["problems"]["parser_mismatch"])
    assert "缺少 provenance.parse_report.scan_pages" in detail


def test_review4_hybrid_with_empty_marker_is_caught(tmp_path):
    """混合件两侧 parser/ocr_derived 均为空：文件级要求核验，缺标记即失败。"""
    assets = make_ocr_corpus(tmp_path, declared="mixed-text-ocr-page-v1",
                             unit_parser="mineru-ocr-page-v1", page_number=1,
                             plan_scan_pages=[1])
    with closing(sqlite3.connect(assets["bm25_path"])) as database:
        database.execute("UPDATE chunks SET parser = NULL, ocr_derived = NULL")
        database.commit()
    payload = {key: value for key, value in assets["payload"].items()
               if key not in {"parser", "ocr_derived"}}
    report = audit_with(assets, payload=payload)
    assert report["status"] == "FAIL", "混合件两侧标记为空却通过了准入"
    assert report["problems"]["ocr_marker_mismatch"], report["problems"]
    assert report["parser_marker"]["verifiable"] is False


def test_review4_ocr_parser_without_marker_value_is_not_counted_as_verified(tmp_path):
    """parser 在但 ocr_derived 缺失：不能计为"已核验"。"""
    assets = make_ocr_corpus(tmp_path, declared="mineru-ocr-page-v1",
                             unit_parser="mineru-ocr-page-v1")
    with closing(sqlite3.connect(assets["bm25_path"])) as database:
        database.execute("UPDATE chunks SET ocr_derived = NULL")
        database.commit()
    payload = {**assets["payload"]}
    payload.pop("ocr_derived")
    report = audit_with(assets, payload=payload)

    assert report["status"] == "FAIL", report["problems"]
    assert report["problems"]["ocr_marker_mismatch"], report["problems"]
    marker = report["parser_marker"]
    assert marker["ocr_source_chunks"] == 1
    assert marker["ocr_marker_verified_chunks"] == 0
    assert marker["verifiable"] is False


def test_review4_ocr_marker_mismatch_between_sides_is_caught(tmp_path):
    """只改一侧的 OCR 标记：两侧逐字段比较必须发现。"""
    assets = make_ocr_corpus(tmp_path, declared="mineru-ocr-page-v1",
                             unit_parser="mineru-ocr-page-v1")
    report = audit_with(assets, payload={**assets["payload"], "ocr_derived": False})
    assert report["problems"]["ocr_marker_mismatch"]


def test_review4_parser_present_on_only_one_side_is_caught(tmp_path):
    """一侧有 parser、另一侧没有：不能让缺失的一侧默认通过。"""
    assets = make_ocr_corpus(tmp_path, declared="mineru-ocr-page-v1",
                             unit_parser="mineru-ocr-page-v1")
    payload = {**assets["payload"]}
    payload.pop("parser")
    payload.pop("ocr_derived")
    report = audit_with(assets, payload=payload)
    assert report["problems"]["parser_mismatch"]


def _null_out_parser_marker(assets: dict) -> None:
    """把两列都置成 NULL（列还在，值没了）——只检查"列在不在"会漏掉这种。"""
    with closing(sqlite3.connect(assets["bm25_path"])) as database:
        database.execute("UPDATE chunks SET parser = NULL, ocr_derived = NULL")
        database.commit()


def test_review4_ocr_source_with_null_marker_on_both_sides_is_caught(tmp_path):
    """失效开放：manifest 声明 OCR 来源，索引两侧却都没有标记值。

    只检查"parser 列在不在"是查不出来的——列在、值是 NULL 时两侧都拿不出标记，
    一段识别文本就会以原文的样子通过准入。
    """
    assets = make_ocr_corpus(tmp_path, declared="mineru-ocr-page-v1",
                             unit_parser="mineru-ocr-page-v1")
    _null_out_parser_marker(assets)
    payload = {key: value for key, value in assets["payload"].items()
               if key not in {"parser", "ocr_derived"}}

    report = audit_with(assets, payload=payload)
    assert report["status"] == "FAIL", "两侧标记为空却通过了准入"
    assert report["problems"]["ocr_marker_mismatch"], report["problems"]
    details = " ".join(item["detail"] for item in report["problems"]["ocr_marker_mismatch"])
    assert "manifest声明该文件含OCR正文" in details


def test_review4_null_marker_is_not_reported_as_verifiable(tmp_path):
    """`verifiable` 必须按**实际核验到的 chunk 数**判定，不能按"列在不在"。"""
    assets = make_ocr_corpus(tmp_path, declared="mineru-ocr-page-v1",
                             unit_parser="mineru-ocr-page-v1")
    _null_out_parser_marker(assets)
    payload = {key: value for key, value in assets["payload"].items()
               if key not in {"parser", "ocr_derived"}}
    report = audit_with(assets, payload=payload)

    marker = report["parser_marker"]
    assert marker["column_present"] is True   # 列在——旧判据会因此说"可核验"
    assert marker["ocr_source_chunks"] == 1
    assert marker["ocr_marker_verified_chunks"] == 0
    assert marker["verifiable"] is False


def test_review4_text_layer_index_reports_zero_ocr_chunks(tmp_path):
    """对照：文本层语料没有 OCR chunk，`verifiable` 为真不是"检查过了"，
    而是"没有需要核验的对象"。两个计数为 0 说明了这一点。
    """
    assets = make_ocr_corpus(tmp_path, declared="pymupdf-page-v1",
                             unit_parser="pymupdf-page-v1")
    report = audit_with(assets)
    marker = report["parser_marker"]
    assert report["status"] == "PASS"
    assert marker["ocr_source_chunks"] == 0
    assert marker["ocr_marker_verified_chunks"] == 0


def test_review4_marker_check_is_not_a_no_op_for_ocr_sources(tmp_path):
    """两侧都如实标注时必须通过——否则上面的拦截可能只是"永远失败"。"""
    assets = make_ocr_corpus(tmp_path, declared="mineru-ocr-page-v1",
                             unit_parser="mineru-ocr-page-v1")
    report = audit_with(assets)
    assert report["status"] == "PASS", report["problems"]
    assert report["parser_marker"]["ocr_marker_verified_chunks"] == 1
