"""可复现的解析对照实验：不依赖模型生成评分，不建立检索索引。

uv run python scripts/run_parse_lab.py
原始样本必须先存在；从原页生成 image-only PDF，明确标记为控制实验衍生件。
"""

import hashlib
import importlib.metadata
import json
import time
from dataclasses import asdict
from pathlib import Path

import pymupdf

from wenjin.ingestion.parsers import parse_pdf

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data/packages/parse-lab"
OUTPUT = ROOT / "artifacts/runs/parse-lab-001"
REVISION = "4c64b92d5caccd71c645e98e0fabb0c4dba7ff45"
SOURCE = f"https://github.com/jsvine/pdfplumber/blob/{REVISION}/examples/pdfs/background-checks.pdf"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    # 内容不变，只去掉文字层。它不是从真实扫描仪采集的复杂扫描件。
    with pymupdf.open(SAMPLES / "original.pdf") as original:
        page = original[0]
        pix = page.get_pixmap(dpi=180)
        pix.save(SAMPLES / "page-image.png")
        with pymupdf.open() as scanned:
            scanned_page = scanned.new_page(width=page.rect.width, height=page.rect.height)
            scanned_page.insert_image(scanned_page.rect, pixmap=pix)
            scanned.save(SAMPLES / "scan.pdf", no_new_id=True)

    write_json(
        SAMPLES / "manifest.json",
        {
            "purpose": "parser teaching fixtures; not production corpus or held-out benchmark",
            "upstream_url": SOURCE,
            "upstream_revision": REVISION,
            "physical_pages": 1,
            "source_note": "Real PDF from pdfplumber official example; raster derivatives generated locally",
            "files": [
                {
                    "path": filename,
                    "sha256": hashlib.sha256((SAMPLES / filename).read_bytes()).hexdigest(),
                    "derived": filename != "original.pdf",
                    "transform": None
                    if filename == "original.pdf"
                    else "page 1 rendered at 180 dpi; no text layer",
                }
                for filename in ["original.pdf", "scan.pdf", "page-image.png"]
            ],
        },
    )

    experiments = [
        ("original-pymupdf", "original.pdf", "pymupdf", None),
        ("original-plumber-lines", "original.pdf", "pdfplumber", None),
        (
            "original-plumber-text-rows",
            "original.pdf",
            "pdfplumber",
            {
                "vertical_strategy": "lines",
                "horizontal_strategy": "text",
                "intersection_tolerance": 5,
            },
        ),
        ("scan-pymupdf", "scan.pdf", "pymupdf", None),
        ("scan-plumber", "scan.pdf", "pdfplumber", None),
    ]
    report = {
        "versions": {p: importlib.metadata.version(p) for p in ["pymupdf", "pdfplumber"]},
        "experiments": [],
    }
    for name, filename, backend, settings in experiments:
        start = time.perf_counter()
        pages = parse_pdf(SAMPLES / filename, backend, table_settings=settings)
        elapsed = time.perf_counter() - start
        write_json(OUTPUT / f"{name}.json", [asdict(page) for page in pages])
        (OUTPUT / f"{name}.txt").write_text(
            "\n\n".join(page.text for page in pages), encoding="utf-8"
        )
        # 字符数不是准确率，仅用来判断文字层是否被读取；行列形状也不是表格正确率。
        report["experiments"].append(
            {
                "name": name,
                "settings": settings,
                "seconds": round(elapsed, 3),
                "text_chars": sum(len(page.text) for page in pages),
                "tables": [
                    {"rows": len(t.rows), "max_columns": max(map(len, t.rows), default=0)}
                    for page in pages
                    for t in page.tables
                ],
                "warnings": [w for page in pages for w in page.warnings],
            }
        )
        # 检查 LlamaIndex 的适配契约确实保留物理页号和来源。
        assert pages[0].to_document().metadata["page_number"] == 1
    write_json(OUTPUT / "summary.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
