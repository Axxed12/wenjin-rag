"""候选 BM25 索引验证：在真拷贝的临时根里建库，与现役索引对比。

三类证据，对应语料的两种现实：

1. **真实 OSG 语料**（307 chunk，实测 0 个含中文）：候选索引与现役索引的 chunk_id
   序列、除 retrieval_text 外所有列、英文 BM25 得分必须逐条/逐位一致。真实语料没有
   中文，所以"中文召回"在这里**不可能**被演示——任何"候选能召回中文"的说法都只能是
   合成语料上的结论，本脚本分开呈现，不混为一谈。
2. **真实语料 + 追加中文文档**（仍是真拷贝根，只改副本 manifest）：走同一个 CLI
   构建，证明中文查询在切分后能召回、在不切分基线上返回 0。
3. **准入检查**：对候选 A（纯真实语料）跑，并与"不切分基线"对照。

约束：不写真实 `corpora/`、不切 active 索引、不覆盖 001/002 候选索引。
临时根一律 `shutil.copytree` 真拷贝，并在建完夹具后断言夹具内链接数为 0
（junction / 硬链接会和真实文件同一个 inode，"只读"只是约定不是保护）。
真实索引、真实 Qdrant、真实 manifest 全部只读。

用法：`./.venv/Scripts/python.exe artifacts/chinese-bm25/verify_candidate.py`
输出：stdout 一份 JSON 摘要；候选 A 另存到 artifacts/chinese-bm25/。
"""

import json
import runpy
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

TARGET_REL = "configs/indexes/osg-3.6.5-blocks-dualhash.json"
BUILD_SCRIPT = ROOT / "scripts/build_bm25_index.py"
BM25_REL = "data/indexes/osg_3_6_5_bm25_blocks_dualhash.sqlite"
ACTIVE_BM25 = ROOT / BM25_REL
OUT_DIR = ROOT / "artifacts/chinese-bm25"
CANDIDATE_NAME = "candidate-bm25-osg-3.6.5-blocks.sqlite"

# 现役索引建库时的列（它没有 parser/ocr_derived 等新列）。retrieval_text 是唯一被
# 索引的列，也是唯一允许与现役不同的列。
ROW_COLUMNS = [
    "chunk_id", "source_path", "version", "start_line", "end_line", "strategy",
    "content_sha256", "source_raw_sha256", "source_canonical_sha256", "normalization",
    "raw_text",
]

ENGLISH_QUERIES = [
    "addChild", "Drawable", "ref_ptr", "osg::Geode", "Texture2D",
    "addDrawable", "Node Visitor", "readRefImageFile", "Group setReferenceFrame",
]

CHINESE_QUERIES = [
    "接口", "用户信息", "查询用户信息", "如何添加对象", "节点遍历",
    "纹理单元", "图片加载", "示例代码",
]

# 追加到真实 manifest 副本里的中文文档。内容刻意用 OSG 相关的说法，但它们是
# **合成语料**，不是上游资料，报告里必须这么标注。
SYNTHETIC_DOCS = {
    "docs-zh/api-guide.md": (
        "# 接口说明\n"
        "\n"
        "本接口用于查询用户信息，返回当前场景里的节点数量。\n"
        "调用方需要先设置纹理单元，再绑定二维纹理。\n"
        "\n"
        "## 示例代码\n"
        "\n"
        "下面的示例代码展示如何添加对象到场景图。\n"
    ),
    "docs-zh/traversal.md": (
        "# 节点遍历\n"
        "\n"
        "节点遍历使用访问者模式，遍历时会依次访问每个子节点。\n"
        "如果想要加载图片，可以先读取图片文件再绑定到纹理单元。\n"
    ),
    "docs-zh/faq.md": (
        "# 常见问题\n"
        "\n"
        "如何添加对象？调用添加子节点的接口即可。\n"
        "查询用户信息请使用本接口说明里的方法。\n"
    ),
}

# 不切分基线：把 segment_cjk 换成恒等函数再跑同一个建库脚本，用来把
# "中文切分带来的差异"和"当前代码与现役索引之间早就存在的差异"分开。
UNSEGMENTED_DRIVER = (
    "import runpy, sys\n"
    "sys.path.insert(0, r'{root}')\n"
    "sys.path.insert(0, r'{src}')\n"
    "from wenjin.indexing import bm25\n"
    "bm25.segment_cjk = lambda text: text\n"
    "sys.argv = ['build_bm25_index.py', '--root', r'{work}', '--target', r'{target}']\n"
    "runpy.run_path(r'{script}', run_name='__main__')\n"
)


def make_temp_root(base: Path, name: str = "root") -> Path:
    """真拷贝夹具根；只拷建库真正要读的三棵子树。"""
    work = base / name
    for rel in ("corpora/osg-3.6.5", "raw_data/osg", "configs"):
        shutil.copytree(ROOT / rel, work / rel)
    return work


def find_links(work: Path) -> list[tuple[str, object]]:
    """夹具里任何硬链接 / 重解析点都要报出来：它们会写到真实仓库上。"""
    found = []
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for path in work.rglob("*"):
        if not path.is_file():
            continue
        info = path.stat()
        if info.st_nlink != 1:
            found.append((str(path.relative_to(work)), info.st_nlink))
        if reparse and getattr(info, "st_file_attributes", 0) & reparse:
            found.append((str(path.relative_to(work)), "REPARSE_POINT"))
    return found


def read_rows(path: Path, columns: list[str]) -> list[tuple]:
    with closing(sqlite3.connect(path)) as database:
        present = {row[1] for row in database.execute("PRAGMA table_info(chunks)")}
        select = ", ".join(
            column if column in present else f"NULL AS {column}" for column in columns
        )
        return [tuple(row) for row in database.execute(
            f"SELECT {select} FROM chunks ORDER BY rowid"
        )]


def read_column(path: Path, column: str) -> dict:
    with closing(sqlite3.connect(path)) as database:
        return dict(database.execute(f"SELECT chunk_id, {column} FROM chunks"))


def cjk_chunk_count(path: Path) -> int:
    from wenjin.indexing.zh import CJK_RUN

    return sum(1 for text in read_column(path, "raw_text").values()
               if text and CJK_RUN.search(text))


def build(work: Path, *, segment: bool) -> None:
    if segment:
        command = [sys.executable, str(BUILD_SCRIPT), "--root", str(work),
                   "--target", TARGET_REL]
    else:
        driver = UNSEGMENTED_DRIVER.format(root=ROOT, src=ROOT / "src", work=work,
                                           target=TARGET_REL, script=BUILD_SCRIPT)
        command = [sys.executable, "-c", driver]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8")
    if result.returncode != 0:
        raise SystemExit(f"建库失败(segment={segment})：{result.stderr}")


def add_chinese_documents(work: Path) -> list[str]:
    """往**副本** manifest 里追加中文文件；真实 manifest 一个字节都不碰。"""
    from wenjin.content_hashing import TEXT_NEWLINE_LF_V1, hash_content

    manifest_path = work / "corpora/osg-3.6.5/source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    raw_root = work / "raw_data" / manifest["raw_data_subdir"]
    added = []
    for relative, text in SYNTHETIC_DOCS.items():
        target = raw_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        hashed = hash_content(target.read_bytes(), normalization=TEXT_NEWLINE_LF_V1,
                              encoding="utf-8")
        manifest["files"].append({
            "path": relative,
            "bytes": target.stat().st_size,
            "indexable": True,
            "source_url": f"https://example.invalid/synthetic/{relative}",
            "raw_sha256": hashed.raw_sha256,
            "canonical_sha256": hashed.canonical_sha256,
            "encoding": "utf-8",
            "normalization": TEXT_NEWLINE_LF_V1,
        })
        added.append(relative)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    return added


def admission(target, root: Path, client) -> dict:
    from wenjin.index_audit import audit_index

    report = audit_index(target, root=root, qdrant_client=client)
    return {
        "status": report.get("status"),
        "nonempty_problems": {kind: len(items)
                              for kind, items in report.get("problems", {}).items() if items},
    }


def score_signature(hits: list[dict]) -> list[tuple]:
    """(chunk_id, 原生 bm25 浮点) 序列——"得分逐位一致"要比的就是这个。

    不能拿整个结果字典比：现役索引没有 parser/ocr_derived 列，候选有，这两个字段
    必然不同，但那是既有的 schema 漂移，与切分无关（下面 diff_fields 会写出来）。
    """
    return [(hit["chunk_id"], hit["raw_bm25"]) for hit in hits]


def english_and_chinese(bm25_path: Path, *, baseline: Path | None = None) -> tuple[dict, dict]:
    from wenjin.indexing.bm25 import bm25_query

    english = {}
    for query in ENGLISH_QUERIES:
        hits = bm25_query(bm25_path, query, top_k=10)
        active_hits = bm25_query(ACTIVE_BM25, query, top_k=10)
        english[query] = {
            "hits": len(hits),
            "scores": [round(hit["score"], 10) for hit in hits],
            "scores_bit_identical_to_active": score_signature(hits)
            == score_signature(active_hits),
            # 文档集合不同时（追加了中文文档）BM25 的 IDF 会随总数 N 变，
            # 得分必然整体平移；这时要看排名是否仍然一致。
            "rank_identical_to_active": [hit["chunk_id"] for hit in hits]
            == [hit["chunk_id"] for hit in active_hits],
            "result_dict_diff_fields": sorted(
                {key for x, y in zip(hits, active_hits) for key in x if x[key] != y[key]}
            ),
        }
    chinese = {}
    for query in CHINESE_QUERIES:
        baseline_hits = bm25_query(baseline, query, top_k=5) if baseline else None
        hits = bm25_query(bm25_path, query, top_k=5)
        chinese[query] = {
            "active_hits": len(bm25_query(ACTIVE_BM25, query, top_k=5)),
            "candidate_hits": len(hits),
            "candidate_terms": hits[0]["matched_terms"] if hits else [],
            "candidate_top": [
                {"chunk_id": hit["chunk_id"], "source_path": hit["source_path"],
                 "score": round(hit["score"], 6),
                 "start_line": hit["start_line"], "end_line": hit["end_line"],
                 "text_head": hit["text"][:40]}
                for hit in hits[:3]
            ],
        }
        if baseline is not None:
            chinese[query]["unsegmented_baseline_hits"] = len(baseline_hits)
    return english, chinese


def remove_tree(path: Path) -> None:
    """Windows 上 sqlite 句柄可能还没释放；重试几次，清不掉就如实报出来。"""
    for _ in range(6):
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            time.sleep(0.5)


def verify_real_corpus(client, base: Path, report: dict) -> None:
    from wenjin.index_registry import load_target

    work = make_temp_root(base, "real")
    report["fixture_links"] = find_links(work)
    if report["fixture_links"]:
        raise SystemExit(f"夹具里存在链接，拒绝继续：{report['fixture_links']}")
    build(work, segment=True)
    candidate = work / BM25_REL
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(candidate, OUT_DIR / CANDIDATE_NAME)
    report["candidate_copy"] = f"artifacts/chinese-bm25/{CANDIDATE_NAME}"

    baseline_root = make_temp_root(base, "real-baseline")
    build(baseline_root, segment=False)
    baseline = baseline_root / BM25_REL

    active_ids = [row[0] for row in read_rows(ACTIVE_BM25, ["chunk_id"])]
    candidate_ids = [row[0] for row in read_rows(candidate, ["chunk_id"])]
    report["chunk_ids"] = {
        "active": len(active_ids),
        "candidate": len(candidate_ids),
        "sequence_identical": active_ids == candidate_ids,
        "set_identical": set(active_ids) == set(candidate_ids),
    }
    report["non_retrieval_columns_identical"] = (
        read_rows(ACTIVE_BM25, ROW_COLUMNS) == read_rows(candidate, ROW_COLUMNS)
    )

    active_texts = read_column(ACTIVE_BM25, "retrieval_text")
    candidate_texts = read_column(candidate, "retrieval_text")
    baseline_texts = read_column(baseline, "retrieval_text")
    report["retrieval_text"] = {
        "total_chunks": len(active_texts),
        "changed_vs_active": sum(1 for cid in active_texts
                                 if active_texts[cid] != candidate_texts[cid]),
        "baseline_equals_active": baseline_texts == active_texts,
    }
    report["corpus_cjk_chunks"] = cjk_chunk_count(ACTIVE_BM25)

    english, chinese = english_and_chinese(candidate, baseline=baseline)
    report["english_queries"] = english
    report["english_scores_all_bit_identical_to_active"] = all(
        item["scores_bit_identical_to_active"] for item in english.values()
    )
    report["chinese_queries_on_real_corpus"] = chinese
    report["real_corpus_admission"] = admission(load_target(work / TARGET_REL), work, client)
    report["real_corpus_baseline_admission"] = admission(
        load_target(baseline_root / TARGET_REL), baseline_root, client)
    report["active_admission"] = admission(load_target(ROOT / TARGET_REL), ROOT, client)


def verify_chinese_recall(base: Path, report: dict) -> None:
    work = make_temp_root(base, "zh")
    added = add_chinese_documents(work)
    report["synthetic_documents"] = {
        "note": "合成语料，不是上游资料；只加在临时根的 manifest 副本里",
        "paths": added,
    }
    build(work, segment=True)
    candidate = work / BM25_REL

    baseline_root = make_temp_root(base, "zh-baseline")
    add_chinese_documents(baseline_root)
    build(baseline_root, segment=False)
    baseline = baseline_root / BM25_REL

    active_ids = [row[0] for row in read_rows(ACTIVE_BM25, ["chunk_id"])]
    candidate_ids = [row[0] for row in read_rows(candidate, ["chunk_id"])]
    report["augmented_chunk_ids"] = {
        "total": len(candidate_ids),
        "active_prefix_preserved": candidate_ids[:len(active_ids)] == active_ids,
        "extra": len(candidate_ids) - len(active_ids),
    }

    english, chinese = english_and_chinese(candidate, baseline=baseline)
    report["augmented_english_queries"] = english
    report["augmented_english_scores_all_bit_identical_to_active"] = all(
        item["scores_bit_identical_to_active"] for item in english.values()
    )
    report["augmented_english_ranks_all_identical_to_active"] = all(
        item["rank_identical_to_active"] for item in english.values()
    )
    report["chinese_queries_on_augmented_corpus"] = chinese


def main() -> None:
    from qdrant_client import QdrantClient
    from wenjin.settings import Settings

    settings = Settings()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    report: dict = {"active_bm25": BM25_REL}
    base = Path(tempfile.mkdtemp(prefix="wenjin-bm25-verify-"))
    try:
        verify_real_corpus(client, base, report)
        verify_chinese_recall(base, report)
    finally:
        client.close()
        remove_tree(base)
        report["leftover_temp_dirs"] = [str(base)] if base.exists() else []

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
