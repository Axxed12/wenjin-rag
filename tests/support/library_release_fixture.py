"""构造两版可发布/回退的合成资料库资产。

调用方必须传入独立临时项目根 `root`。本夹具只在该根下写两份 synthetic
manifest/raw 文件、两份 BM25 SQLite 和一个 registry，并在函数内部创建真正的
Qdrant `:memory:` 客户端。调用时不加载 Settings 或项目 `.env`，且读写均显式传入临时 root；不依赖默认语料目录，
也不访问真实 archives 或冻结评测。返回的 Qdrant client 由调用方关闭，以便同一
进程中的 HTTP/MCP 服务复用同一组内存 collection。

最小用法：

    assets = build_two_version_release_fixture(root=tmp_path / "release-root")
    try:
        old = assets["targets"]["A"]
        new = assets["targets"]["B"]
        assert assets["registry"].active() == old
        # 在同一进程把 assets["qdrant"] 传给待测 HTTP/MCP 装配器。
    finally:
        assets["qdrant"].close()

`old`、`new` 是 IndexTarget；`queries` 和 `expected_markers` 给后续请求验收使用。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

from wenjin.content_hashing import (
    TEXT_NEWLINE_LF_V1,
    hash_content,
    manifest_content_fingerprint,
)
from wenjin.index_audit import admit_index
from wenjin.index_registry import IndexRegistryStore
from wenjin.indexing.build import build_candidate_index

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIBRARY_ID = "synthetic-release-library"
_STRATEGY = "blocks-split"
_DIMENSION = 8


class _MarkerEmbedder:
    """不加载模型的确定性向量桩，只供建 synthetic 索引。"""

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [((value / 255.0) * 2.0) - 1.0 for value in digest[:_DIMENSION]]

    def get_text_embedding_batch(
        self, texts: list[str], *, show_progress: bool = False
    ) -> list[list[float]]:
        del show_progress
        return [self._vector(text) for text in texts]

    def get_query_embedding(self, query: str) -> list[float]:
        return self._vector(query)


def _write_release_source(
    root: Path,
    *,
    library_id: str,
    label: str,
    version: str,
    marker: str,
    owned_paths: list[Path],
) -> Path:
    corpus_id = f"{library_id}-{version}"
    raw_subdir = f"{library_id}/{version}"
    raw_path = root / "raw_data" / raw_subdir / "release-guide.txt"
    manifest_path = root / "corpora" / corpus_id / "source-manifest.json"
    content = (
        f"Synthetic release {label}, version {version}.\n"
        f"Unique retrieval marker: {marker}.\n"
    ).encode("utf-8")
    hashed = hash_content(
        content, normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8"
    )
    manifest = {
        "schema_version": 3,
        "corpus_id": corpus_id,
        "library": library_id,
        "version": version,
        "license": "synthetic-test-only",
        "source_url": f"urn:synthetic:{library_id}:{version}",
        "raw_data_subdir": raw_subdir,
        "files": [
            {
                "path": "release-guide.txt",
                "raw_sha256": hashed.raw_sha256,
                "canonical_sha256": hashed.canonical_sha256,
                "normalization": TEXT_NEWLINE_LF_V1,
                "encoding": "utf-8",
                "bytes": len(content),
                "indexable": True,
                "parser": "text-line-v1",
                "source_url": f"urn:synthetic:{library_id}:{version}/release-guide.txt",
                "logical_document_id": f"{corpus_id}:release-guide.txt",
            }
        ],
    }
    manifest["canonical_corpus_sha256"] = manifest_content_fingerprint(manifest)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    # x 模式再次确认目标仍为空，避免预检之后覆盖并发产生的文件。
    with raw_path.open("xb") as output:
        owned_paths.append(raw_path)
        output.write(content)
    with manifest_path.open("xb") as output:
        owned_paths.append(manifest_path)
        output.write(
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        )
    return manifest_path


def build_two_version_release_fixture(
    *, root: Path, library_id: str = _LIBRARY_ID,
    namespace: str | None = None,
    qdrant_client: QdrantClient | None = None,
) -> dict[str, Any]:
    """在调用方指定的空白测试子目录中生成 A/B 两版完整候选资产。

    输入：显式临时项目根 `root`，不得传仓库根或真实数据目录。
    返回：包含 targets A/B、当前 active A 的 registry、同进程真实 in-memory
    Qdrant client、marker 查询和所有写入路径的字典。可指定synthetic library_id
    以覆盖默认OSG路由合同；在同一root构造多个库时，给后续库传独立namespace，
    并可显式复用同一个Qdrant client。若未传客户端，调用方负责关闭新建客户端。
    写入边界：只创建下方预检列出的 manifest/raw/BM25/registry 与对应内存 collection；
    遇到同名任意文件或 collection 即拒绝，不覆盖调用方现存内容。
    """
    resolved_root = Path(root).resolve()
    if resolved_root == _REPO_ROOT or resolved_root.is_relative_to(_REPO_ROOT):
        raise ValueError("release fixture 的 root 必须位于仓库之外的独立临时目录")
    resolved_root.mkdir(parents=True, exist_ok=True)

    if not library_id or any(character in library_id for character in "\\/\0"):
        raise ValueError("fixture library_id必须为有效、简单的测试标识")
    if namespace is not None and (
        not namespace or any(character in namespace for character in "\\/\0")
    ):
        raise ValueError("fixture namespace必须为有效、简单的测试标识")

    specifications = {
        "A": {"version": "1.0", "marker": "release_marker_A"},
        "B": {"version": "2.0", "marker": "release_marker_B"},
    }
    paths: dict[str, dict[str, Path]] = {}
    for label, item in specifications.items():
        version = item["version"]
        corpus_id = f"{library_id}-{version}"
        file_namespace = namespace or ""
        filename_prefix = f"{file_namespace}-" if file_namespace else ""
        paths[label] = {
            "raw": resolved_root / "raw_data" / library_id / version / "release-guide.txt",
            "manifest": resolved_root / "corpora" / corpus_id / "source-manifest.json",
            "bm25": resolved_root / "data" / "indexes" / f"{filename_prefix}release-{label.lower()}.sqlite",
        }
    registry_name = f"{namespace}-index-registry.json" if namespace else "index-registry.json"
    registry_path = resolved_root / "data" / "indexes" / registry_name
    expected_paths = [
        registry_path,
        *(path for release in paths.values() for path in release.values()),
    ]
    occupied = [path for path in expected_paths if path.exists()]
    if occupied:
        raise FileExistsError(f"拒绝覆盖已存在的 fixture 输入/资产：{occupied[0]}")

    collection_prefix = f"synthetic_{namespace}" if namespace else "synthetic_release"
    collection_names = {
        label: f"{collection_prefix}_{label.lower()}" for label in specifications
    }
    owns_qdrant = qdrant_client is None
    qdrant = qdrant_client or QdrantClient(":memory:")
    occupied_collections = [
        name for name in collection_names.values() if qdrant.collection_exists(name)
    ]
    if occupied_collections:
        if owns_qdrant:
            qdrant.close()
        raise FileExistsError(f"拒绝覆盖已存在的fixture collection：{occupied_collections[0]}")
    targets = {}
    owned_paths: list[Path] = []
    try:
        embedder = _MarkerEmbedder()
        for label, item in specifications.items():
            manifest_path = _write_release_source(
                resolved_root,
                library_id=library_id,
                label=label,
                version=item["version"],
                marker=item["marker"],
                owned_paths=owned_paths,
            )
            targets[label] = build_candidate_index(
                manifest_path=manifest_path,
                target_id=f"{library_id}-release-{label.lower()}",
                strategy=_STRATEGY,
                dense_collection=collection_names[label],
                bm25_path=paths[label]["bm25"],
                qdrant_client=qdrant,
                embedder=embedder,
                embedding_model="synthetic-marker-embedder-v1",
                project_root=resolved_root,
            )
            owned_paths.append(paths[label]["bm25"])
            # 先逐版通过产品准入，确保 fixture 不是只在自测 builder 里自洽。
            admit_index(targets[label], root=resolved_root, qdrant_client=qdrant)

        registry = IndexRegistryStore(registry_path)
        registry.initialize(
            targets["A"], reason="synthetic release fixture starts on marker A"
        )
        owned_paths.append(registry_path)
        return {
            "root": resolved_root,
            "qdrant": qdrant,
            "embedder": embedder,
            "registry": registry,
            "registry_path": registry_path,
            "targets": targets,
            "active_target": targets["A"],
            "library_id": library_id,
            "queries": {label: item["marker"] for label, item in specifications.items()},
            "expected_markers": {label: item["marker"] for label, item in specifications.items()},
            "paths": paths,
            "collections": collection_names,
            "classification": "synthetic-two-release-fixture-not-retrieval-quality-validation",
        }
    except Exception:
        for collection in collection_names.values():
            if qdrant.collection_exists(collection):
                qdrant.delete_collection(collection)
        if owns_qdrant:
            qdrant.close()
        # 只清除通过独占创建或成功建索引明确拥有的文件。
        for path in owned_paths:
            path.unlink(missing_ok=True)
        raise

