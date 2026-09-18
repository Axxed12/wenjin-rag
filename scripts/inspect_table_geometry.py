"""查看同一张 PDF 表格在不同解析策略下的几何边界。

这个脚本只打印前几行和前几列，避免输出整张表。
运行：uv run python scripts/inspect_table_geometry.py
"""

from pathlib import Path

import pdfplumber

PDF_PATH = Path(__file__).resolve().parents[1] / "data/packages/parse-lab/original.pdf"


CONFIGS = [
    ("default", {}),
    (
        "text-rows",
        {
            "vertical_strategy": "lines",
            "horizontal_strategy": "text",
            "intersection_tolerance": 5,
        },
    ),
]


def show_table(name: str, settings: dict) -> None:
    """打印一个策略检测到的表格、行边界和单元格边界。"""
    with pdfplumber.open(PDF_PATH) as pdf:
        table = pdf.pages[0].find_tables(settings)[0]

        print(f"\n--- {name} ---")
        print("table bbox:", tuple(round(value, 1) for value in table.bbox))
        print("rows:", len(table.rows), "columns in first row:", len(table.rows[0].cells))

        for row_index, row in enumerate(table.rows[:6]):
            # 每个 cell 是 (x0, top, x1, bottom)，None 表示这个位置不是完整单元格。
            cells = []
            for cell_index, cell in enumerate(row.cells[:3]):
                text = table.extract()[row_index][cell_index]
                bbox = None if cell is None else tuple(round(value, 1) for value in cell)
                cells.append({"column": cell_index, "text": repr(text), "bbox": bbox})
            print("row", row_index, cells)


def main() -> None:
    for name, settings in CONFIGS:
        show_table(name, settings)


if __name__ == "__main__":
    main()
