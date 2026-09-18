"""向已有内网 MinerU 提交同一实验扫描件，保存输入指纹与原始输出。

uv run python scripts/run_mineru_lab.py
只执行 pipeline + OCR，参数固定；不自动切换其他后端掩盖失败。
"""

import argparse
import hashlib
import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    source = ROOT / "data/packages/parse-lab/scan.pdf"
    output = ROOT / "artifacts/runs/parse-lab-001"
    output.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    params = {
        "backend": "pipeline",
        "parse_method": "ocr",
        "return_md": "true",
        "return_content_list": "true",
        "formula_enable": "false",
        "table_enable": "true",
    }
    record = {"input_sha256": hashlib.sha256(data).hexdigest(), "parameters": params}
    started = time.monotonic()
    with httpx.Client(base_url=args.url, timeout=30, trust_env=False) as client:
        response = client.post(
            "/tasks", files={"files": (source.name, data, "application/pdf")}, data=params
        )
        response.raise_for_status()
        task = response.json()
        record["submission"] = task
        # 一旦提交即落盘，超时后可凭 task_id 查询，不盲目重复提交。
        record_path = output / "mineru-task.json"
        record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        while True:
            response = client.get(f"/tasks/{task['task_id']}")
            response.raise_for_status()
            status = response.json()
            if status["status"] in {"completed", "failed", "cancelled"}:
                break
            if time.monotonic() - started > args.timeout:
                raise TimeoutError(
                    f"Task still pending/running; inspect {record_path}; server task not cancelled"
                )
            time.sleep(2)
        record["final_status"] = status
        record["client_seconds"] = round(time.monotonic() - started, 3)
        record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        if status["status"] != "completed":
            raise RuntimeError(f"MinerU task failed: {status.get('error')}")
        response = client.get(f"/tasks/{task['task_id']}/result")
        response.raise_for_status()
        result = response.json()
    (output / "mineru-raw.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = result["results"]["scan"]["md_content"]
    (output / "mineru-output.md").write_text(markdown, encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "completed",
                "seconds": record["client_seconds"],
                "markdown_chars": len(markdown),
                "note": "Completion is not evidence of table accuracy",
            }
        )
    )


if __name__ == "__main__":
    main()
