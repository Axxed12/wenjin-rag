"""用 OCR/文字对象的坐标，恢复样本表格的三列业务记录。

这是教学实验，不是通用 PDF 表格解析器：列边界来自本样本的可视化检查，
只恢复前五个州的 State / Permit / Totals。运行：

    uv run python scripts/recover_table_demo.py
"""

import json
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parents[2]
PDF_PATH = ROOT / "data/packages/parse-lab/original.pdf"
OUTPUT_PATH = ROOT / "artifacts/runs/parse-lab-001/recovered-first-five.json"

# 这些是本页的实验列边界，不应直接推广到其他 PDF。
COLUMNS = {
    "state": (33.1, 111.4),
    "permit": (111.4, 145.8),
    "totals": (953.0, 975.3),
}


def group_words_into_rows(words: list[dict], tolerance: float = 1.5) -> list[list[dict]]:
    """按文字顶部坐标聚类；相邻文字的 top 差值不超过 tolerance 视为同一行。

    这里使用 top，而不是数组下标，因为不同解析策略生成的数组行数不同。
    """
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda item: (item["top"], item["x0"])):
        if not rows or abs(word["top"] - rows[-1][0]["top"]) > tolerance:
            rows.append([word])
        else:
            rows[-1].append(word)
    return rows


def words_in_column(row: list[dict], left: float, right: float) -> str:
    """取文字中心点落入列区域的 words，并按 x 坐标拼接。"""
    selected = [
        word for word in row
        if left <= (word["x0"] + word["x1"]) / 2 < right
    ]
    selected.sort(key=lambda item: item["x0"])
    # 文字被拆成多个 word 时，保留一个空格；这是显示文本，不是数值解析。
    return " ".join(word["text"] for word in selected)


def recover_first_five() -> list[dict]:
    """读取数据区域的文字对象，恢复 5 条记录。"""
    with pdfplumber.open(PDF_PATH) as pdf:
        page = pdf.pages[0]
        # 79.0 到 115.5 是前五条数据的垂直区域，来自本样本检查。
        words = page.extract_words(x_tolerance=1, y_tolerance=1, keep_blank_chars=False)
        data_words = [word for word in words if 79.0 <= word["top"] < 115.5]
        rows = group_words_into_rows(data_words)

    records = []
    for row_number, row in enumerate(rows, start=1):
        state = words_in_column(row, *COLUMNS["state"])
        permit = words_in_column(row, *COLUMNS["permit"])
        totals = words_in_column(row, *COLUMNS["totals"])
        if state:  # 空行或表头不能被误当成业务记录。
            records.append(
                {
                    "row_number_in_experiment": row_number,
                    "state": state,
                    "permit": permit,
                    "totals": totals,
                    "source": {
                        "file": "original.pdf",
                        "page_number": 1,
                        "bbox": [
                            min(word["x0"] for word in row),
                            min(word["top"] for word in row),
                            max(word["x1"] for word in row),
                            max(word["bottom"] for word in row),
                        ],
                    },
                }
            )
    return records


def main() -> None:
    records = recover_first_five()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(records, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
