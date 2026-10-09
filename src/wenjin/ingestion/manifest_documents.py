"""校验 legacy manifest 并读取可索引原文，供建库与 CLI 共用。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from wenjin.content_hashing import TEXT_NEWLINE_LF_V1, hash_content
from wenjin.ingestion.multiformat import parser_for_path

# src-layout 源码运行时 parents[3] 是仓库根目录；不导入 settings 避免加载 torch。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = PROJECT_ROOT / "corpora" / "osg-3.6.5" / "source-manifest.json"
DEFAULT_RAW_DATA_ROOT = PROJECT_ROOT / "raw_data"


def resolve_raw_root(
    manifest_path: Path, manifest: dict, raw_data_root: Path | None = None
) -> Path:
    """把 manifest 声明的语料目录限制在可信 raw_data 根内。"""
    if "raw_data_subdir" in manifest:
        configured_root = raw_data_root or Path(
            os.environ.get("WENJIN_RAW_DATA_ROOT", DEFAULT_RAW_DATA_ROOT)
        )
        if not configured_root.is_absolute():
            configured_root = PROJECT_ROOT / configured_root
        trusted_root = configured_root.resolve()
        subdir = Path(manifest["raw_data_subdir"])
        if subdir.is_absolute():
            raise ValueError("raw_data_subdir必须是相对路径")
        resolved = (trusted_root / subdir).resolve()
        if not resolved.is_relative_to(trusted_root):
            raise ValueError("raw_data_subdir超出raw_data根目录")
        return resolved

    # 旧格式只用于项目内临时实验包；仍将 raw_root 限制在 manifest 所在目录。
    package_root = Path(manifest_path).parent.resolve()
    resolved = (package_root / manifest["raw_root"]).resolve()
    if not resolved.is_relative_to(package_root):
        raise ValueError("raw_root超出临时语料包目录")
    return resolved


def load_documents(
    manifest_path: Path, *, raw_data_root: Path | None = None
) -> list[dict]:
    """返回校验后的完整文档字典；切分和模型调用由后续模块负责。"""
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    raw_root = resolve_raw_root(manifest_path, manifest, raw_data_root)
    documents = []

    for entry in manifest["files"]:
        if not entry["indexable"]:
            continue
        source_path = entry["path"]
        file_path = (raw_root / source_path).resolve()
        if not file_path.is_relative_to(raw_root):
            raise ValueError(f"文件路径超出语料目录：{source_path}")
        if not file_path.is_file():
            raise FileNotFoundError(f"清单中的语料文件不存在：{file_path}")

        raw_bytes = file_path.read_bytes()
        normalization = entry.get("normalization", TEXT_NEWLINE_LF_V1)
        encoding = entry.get("encoding", "utf-8")
        hashed = hash_content(
            raw_bytes, normalization=normalization, encoding=encoding
        )
        expected_raw = entry.get("raw_sha256") or entry.get("sha256")
        if not expected_raw or hashed.raw_sha256 != expected_raw.lower():
            raise ValueError(f"语料校验失败，文件可能被修改：{source_path}")
        expected_canonical = entry.get("canonical_sha256")
        if expected_canonical and hashed.canonical_sha256 != expected_canonical.lower():
            raise ValueError(f"语料规范内容校验失败：{source_path}")

        documents.append(
            {
                "source_path": source_path,
                "version": manifest["version"],
                "source_url": entry["source_url"],
                "sha256": hashed.raw_sha256,
                "raw_sha256": hashed.raw_sha256,
                "canonical_sha256": hashed.canonical_sha256,
                "normalization": hashed.normalization,
                "encoding": hashed.encoding,
                "text": hashed.canonical_text(),
                "parser": parser_for_path(file_path),
                "logical_document_id": entry.get("logical_document_id"),
            }
        )

    return documents


def main() -> None:
    """运行 `python -m wenjin.ingestion.manifest_documents` 检查默认语料。"""
    import argparse

    parser = argparse.ArgumentParser(description="读取并校验 RAG 原始语料")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--raw-data-root", type=Path)
    args = parser.parse_args()
    documents = load_documents(args.manifest, raw_data_root=args.raw_data_root)
    print(f"读取完成：{len(documents)} 份可索引文档")
    print(f"总字符数：{sum(len(doc['text']) for doc in documents):,}")
    if not documents:
        return
    example = next(
        (doc for doc in documents if doc["source_path"] == "include/osg/Geode"),
        documents[0],
    )
    print(f"示例文件：{example['source_path']}")
    print(f"版本：{example['version']}")
    print(f"原始行数：{len(example['text'].splitlines())}")
    print(f"来源：{example['source_url']}")
    print("正文预览（前 300 个字符）：")
    print(example["text"][:300])


if __name__ == "__main__":
    main()
