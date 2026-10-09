"""将坐标恢复实验结果转成带表头上下文的 chunk，供切分实验查看。"""

import json
from pathlib import Path

from wenjin.ingestion.table_text import records_to_chunks

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "artifacts/runs/parse-lab-001/recovered-first-five.json"
OUTPUT = ROOT / "artifacts/runs/parse-lab-001/table-chunks.jsonl"


def main() -> None:
    records = json.loads(INPUT.read_text(encoding="utf-8"))
    chunks = records_to_chunks(
        records,
        title="NICS Firearm Background Checks",
        period="November - 2015",
    )
    OUTPUT.write_text(
        "\n".join(json.dumps(chunk, ensure_ascii=False) for chunk in chunks) + "\n",
        encoding="utf-8",
    )
    print(f"生成 {len(chunks)} 个表格 chunk：{OUTPUT}")
    print(chunks[0]["text"])


if __name__ == "__main__":
    main()
