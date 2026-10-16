"""预取并校验新版MCP解析器构建源码；不读取应用配置或凭据。"""

import hashlib
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

base = Path(__file__).resolve().parents[1] / "data/packages/mcp-lxml-build-20261014"
base.mkdir(parents=True, exist_ok=True)
sources = json.loads((Path(__file__).parent / "lxml-sources.json").read_text(encoding="utf-8"))
urls = [item["url"] for item in sources]
expected = {item["file"]: item["sha256"] for item in sources}


def fetch(url):
    p = base / url.rsplit("/", 1)[-1]
    try:
        cached = p.exists()
        if cached:
            data = p.read_bytes()
        else:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = r.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected[p.name]:
            raise ValueError("Source checksum differs")
        if not cached:
            p.write_bytes(data)
        return {
            "file": p.name,
            "bytes": len(data),
            "sha256": digest,
            "status": "cached" if cached else "downloaded",
        }
    except Exception as e:
        return {"file": p.name, "status": "failed", "error_type": type(e).__name__}


with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(fetch, urls))
(base / "download-manifest.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
print(json.dumps(results))
raise SystemExit(any(item["status"] == "failed" for item in results))
