"""验收门的变异测试：损坏字节、越界路径/证据、泄漏必须被拦截。"""

import json

import pytest

from scripts.audit_external_corpus import audit, hashes, safe_path
from scripts.import_verified_corpus import import_package
from scripts.verify_imported_datasets import answer_atoms, readable
from wenjin.content_hashing import sha256_hex


@pytest.mark.parametrize("value", ["../outside", "C:\\outside", "/outside", "\\\\host\\share"])
def test_path_escape(tmp_path, value):
    with pytest.raises(ValueError):
        safe_path(tmp_path, value)


def test_dual_hash_newlines():
    crlf, lf = hashes(b"a\r\nb\r\n"), hashes(b"a\nb\n")
    assert crlf["raw_sha256"] != lf["raw_sha256"]
    assert crlf["canonical_sha256"] == lf["canonical_sha256"]


def test_answer_atoms_include_dates_versions_identifiers():
    row = {"answer_rubric": "OGC 06-042; 2006-03-15; 1.3.0; KHR_texture_basisu"}
    assert set(answer_atoms(row)) == {
        "OGC 06-042", "2006-03-15", "1.3.0", "KHR_texture_basisu"
    }


def test_roff_escape_does_not_hide_options():
    assert readable(r"\fB\-\-area\-of\-use\fP", ".1") == "--area-of-use"


def test_html_visible_text():
    assert readable('<p id="not-evidence">TRUE &amp; FALSE</p>', ".html") == "TRUE & FALSE"


def test_import_rejects_changed_reviewed_bundle(tmp_path):
    stage = tmp_path / "stage"
    (stage / "raw_data").mkdir(parents=True)
    (stage / "raw_data/a").write_bytes(b"changed")
    with pytest.raises(ValueError, match="审查后发生变化"):
        import_package(stage, tmp_path / "target", {
            "status": "PASS", "approved_files": {"raw_data/a": sha256_hex(b"original")}
        })


def test_import_requires_review(tmp_path):
    with pytest.raises(ValueError, match="缺少通过"):
        import_package(tmp_path / "stage", tmp_path / "target", {"status": "FAIL"})


@pytest.mark.parametrize("mutation", [None, "bytes", "span", "leakage", "seal"])
def test_acceptance_mutations(tmp_path, mutation):
    root = tmp_path / "package"
    (root / "raw").mkdir(parents=True)
    (root / "datasets").mkdir()
    entries = []
    for name, data in [("a.txt", b"alpha\n"), ("b.txt", b"beta\n")]:
        (root / "raw" / name).write_bytes(data)
        entries.append({"local_path": f"raw/{name}", "sha256": sha256_hex(data),
                        "bytes": len(data), "release_date": "2026-01-01"})
    (root / "manifest.draft.json").write_text(json.dumps({
        "entries": entries, "total_files": 2, "total_bytes": 11,
    }))
    for split, i in [("validation", 0), ("test", 1)]:
        source = entries[0 if mutation == "leakage" else i]
        row = {"id": split, "query": ["apple", "zebra"][i], "answer_rubric": split,
               "evidence": [{"path": source["local_path"], "start_line": 1,
                             "end_line": 2 if mutation == "span" else 1,
                             "source_sha256": source["sha256"]}]}
        path = root / "datasets" / f"rag-{split}-001.jsonl"
        data = (json.dumps(row) + "\n").encode()
        path.write_bytes(data)
        path.with_suffix(".sha256").write_text(
            "0" * 64 if mutation == "seal" else sha256_hex(data)
        )
    if mutation == "bytes":
        (root / "raw/a.txt").write_bytes(b"wrong\n")
    result = audit(root, tmp_path / "project")
    assert bool(result["errors"]) == (mutation is not None)
