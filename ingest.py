"""RAG 第一步：按来源清单读取原始文档，并保留可追溯信息。

运行：uv run python ingest.py
这一版只负责读取，不做切分、向量化或模型调用。
"""

import argparse
import json
import os
from pathlib import Path

from wenjin.content_hashing import TEXT_NEWLINE_LF_V1, hash_content

# __file__ 是当前脚本的位置，不是终端的工作目录。
# 因此，从其他目录启动这个脚本也能找到默认的来源清单。
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = PROJECT_ROOT / "corpora" / "osg-3.6.5" / "source-manifest.json"
DEFAULT_RAW_DATA_ROOT = PROJECT_ROOT / "raw_data"


def resolve_raw_root(
    manifest_path: Path, manifest: dict, raw_data_root: Path | None = None
) -> Path:
    """在可信raw_data根目录内解析语料位置；兼容临时实验包的旧格式。"""
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

    # 旧格式只用于项目内临时实验包和单元测试；路径仍限制在manifest目录内。
    package_root = manifest_path.parent.resolve()
    resolved = (package_root / manifest["raw_root"]).resolve()
    if not resolved.is_relative_to(package_root):
        raise ValueError("raw_root超出临时语料包目录")
    return resolved


def load_documents(
    manifest_path: Path, *, raw_data_root: Path | None = None
) -> list[dict]:
    """返回可索引文档列表；缺文件、校验失败或编码错误时直接报错。

    manifest_path：来源清单的路径。
    返回值：每个字典对应一份完整原始文件，还不是 chunk。
    """
    manifest_path = manifest_path.resolve()

    # utf-8-sig 同时兼容普通 UTF-8 和带 BOM 的 UTF-8 清单。
    # 当前清单由 PowerShell 生成，显式指定编码可避免平台默认编码干扰。
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))

    # 正式语料由corpora里的manifest描述，原文件统一放在不提交Git的raw_data目录。
    raw_root = resolve_raw_root(manifest_path, manifest, raw_data_root)
    documents = []

    # 根据清单读取，不用 *.h 搜索：OSG 的许多头文件没有扩展名。
    for entry in manifest["files"]:
        if not entry["indexable"]:
            continue  # LICENSE.txt 保留在原始资料中，但不参与检索。

        source_path = entry["path"]
        file_path = (raw_root / source_path).resolve()

        # 清单里的文件必须位于原始资料目录内，避免错误路径读到其他文件。
        if not file_path.is_relative_to(raw_root):
            raise ValueError(f"文件路径超出语料目录：{source_path}")
        if not file_path.is_file():
            raise FileNotFoundError(f"清单中的语料文件不存在：{file_path}")

        # 先校验原始字节，再校验规范内容；旧manifest的sha256字段仅用于兼容迁移。
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
        # 切片、Embedding和BM25必须共同使用这份规范文本。
        text = hashed.canonical_text()

        # 文本是检索内容，其他字段用于版本过滤、引用和排查问题。
        documents.append(
            {
                "source_path": source_path,
                "version": manifest["version"],
                "source_url": entry["source_url"],
                "sha256": hashed.raw_sha256,  # 兼容旧调用；新代码使用下面的显式字段。
                "raw_sha256": hashed.raw_sha256,
                "canonical_sha256": hashed.canonical_sha256,
                "normalization": hashed.normalization,
                "encoding": hashed.encoding,
                "text": text,
            }
        )

    return documents


def main() -> None:
    """提供命令行入口，打印读取结果，方便检查数据是否正确。"""
    parser = argparse.ArgumentParser(description="读取并校验 RAG 原始语料")
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST, help="来源清单路径"
    )
    parser.add_argument(
        "--raw-data-root",
        type=Path,
        help="原始语料总目录；默认读取WENJIN_RAW_DATA_ROOT或项目raw_data目录",
    )
    args = parser.parse_args()
    documents = load_documents(args.manifest, raw_data_root=args.raw_data_root)

    print(f"读取完成：{len(documents)} 份可索引文档")
    print(f"总字符数：{sum(len(doc['text']) for doc in documents):,}")
    if not documents:
        return

    # 优先展示 Geode，方便与开发题 001 对照；其他语料包则展示首份文件。
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


# 直接运行脚本时执行 main；以后 import load_documents 时不会自动运行。
if __name__ == "__main__":
    main()
