"""按manifest从固定GitHub提交下载原始语料，并在落盘前校验SHA256。

外网机示例：
    uv run python scripts/fetch_corpus.py

内网环境不运行下载；将外网机生成的raw_data目录校验后人工摆渡进去。
"""

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ingest import DEFAULT_MANIFEST, resolve_raw_root  # noqa: E402


def github_raw_url(manifest: dict, source_path: str) -> str:
    """把仓库、固定commit和相对路径组合成不可漂移的raw下载地址。"""
    repository = manifest["repository"].removesuffix("/")
    prefix = "https://github.com/"
    if not repository.startswith(prefix):
        raise ValueError("自动下载当前只支持github.com仓库")
    repository_path = repository.removeprefix(prefix).removesuffix(".git")
    quoted_path = "/".join(urllib.parse.quote(part) for part in Path(source_path).parts)
    return (
        f"https://raw.githubusercontent.com/{repository_path}/"
        f"{manifest['commit']}/{quoted_path}"
    )


def download_bytes(url: str, *, timeout: int, attempts: int = 3) -> bytes:
    """短暂网络错误最多重试三次；仍失败时保留原始异常原因。"""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "wenjin-rag-corpus-fetch/1.0"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return response.read()
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
    raise RuntimeError(f"下载失败，已重试{attempts}次：{url}") from last_error


def materialize(manifest_path: Path, raw_data_root: Path | None, timeout: int) -> dict:
    """已有且哈希正确的文件直接复用；下载内容校验通过后才原子替换。"""
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    destination = resolve_raw_root(manifest_path, manifest, raw_data_root)
    destination.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    reused = 0
    for entry in manifest["files"]:
        target = (destination / entry["path"]).resolve()
        if not target.is_relative_to(destination):
            raise ValueError(f"文件路径超出语料目录：{entry['path']}")
        expected = (entry.get("raw_sha256") or entry.get("sha256")).lower()
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            reused += 1
            continue

        payload = download_bytes(
            github_raw_url(manifest, entry["path"]), timeout=timeout
        )
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise ValueError(f"下载文件SHA256不匹配：{entry['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.part")
        temporary.write_bytes(payload)
        temporary.replace(target)
        downloaded += 1
    return {
        "corpus_id": manifest["corpus_id"],
        "version": manifest["version"],
        "destination": str(destination),
        "files": len(manifest["files"]),
        "downloaded": downloaded,
        "reused": reused,
        "status": "PASS",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="下载并校验manifest指定的公开原始语料")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--raw-data-root", type=Path)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    report = materialize(args.manifest, args.raw_data_root, args.timeout)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
