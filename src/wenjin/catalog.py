"""语料目录：回答"向量库里现在有哪些东西"。

只读 manifest 和索引配置，不碰 Qdrant、不碰 MCP。检索服务、CLI 和 MCP 工具共用同一份
目录结果，避免各自实现一套"有哪些语料"的逻辑。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

PUBLIC_DIR = "corpora"
INTERNAL_DIR = "corpora-private"


@dataclass(frozen=True)
class CorpusEntry:
    corpus_id: str
    library: str
    version: str
    visibility: str
    license: str
    source_url: str | None
    raw_data_subdir: str
    files: int
    retrieval_eligible_files: int
    retrieval_scope_files: tuple[str, ...]
    manifest_path: str
    index_targets: tuple[str, ...] = field(default=())

    @property
    def indexed(self) -> bool:
        return bool(self.index_targets)

    def to_dict(self, *, include_files: bool = True) -> dict:
        payload = {
            "corpus_id": self.corpus_id,
            "library": self.library,
            "version": self.version,
            "visibility": self.visibility,
            "license": self.license,
            "source_url": self.source_url,
            "files": self.files,
            "retrieval_eligible_files": self.retrieval_eligible_files,
            "indexed": self.indexed,
            "index_targets": list(self.index_targets),
        }
        if include_files:
            payload["retrieval_scope_files"] = list(self.retrieval_scope_files)
        return payload


def _manifest_paths(root: Path) -> list[tuple[Path, str]]:
    found = []
    for directory, visibility in ((PUBLIC_DIR, "public"), (INTERNAL_DIR, "internal")):
        base = root / directory
        if not base.is_dir():
            continue
        found.extend(
            (path, visibility) for path in sorted(base.glob("*/source-manifest.json"))
        )
    return found


def _index_targets_by_manifest(root: Path) -> dict[str, list[str]]:
    """从索引配置里反查每个manifest已经建了哪些target。"""
    mapping: dict[str, list[str]] = {}
    config_dir = root / "configs/indexes"
    if not config_dir.is_dir():
        return mapping
    for path in sorted(config_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue
        candidates = data.get("candidates") or [data]
        for item in candidates:
            if not isinstance(item, dict) or "manifest_path" not in item:
                continue
            mapping.setdefault(item["manifest_path"], []).append(item.get("target_id", path.stem))
    return mapping


def corpus_catalog(root: Path) -> list[CorpusEntry]:
    """列出全部语料，按corpus_id排序；索引缺失不影响列出。"""
    targets = _index_targets_by_manifest(root)
    entries = []
    for manifest_path, visibility in _manifest_paths(root):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        relative = manifest_path.relative_to(root).as_posix()
        files = manifest.get("files", [])
        entries.append(CorpusEntry(
            corpus_id=manifest.get("corpus_id", manifest_path.parent.name),
            library=manifest.get("library", "unknown"),
            version=manifest.get("version", "unknown"),
            visibility=visibility,
            license=manifest.get("license", "unknown"),
            source_url=manifest.get("source_url"),
            raw_data_subdir=manifest.get("raw_data_subdir", ""),
            files=len(files),
            retrieval_eligible_files=sum(1 for item in files if item.get("retrieval_eligible")),
            retrieval_scope_files=tuple(
                item["path"] for item in files if item.get("retrieval_eligible")
            ),
            manifest_path=relative,
            index_targets=tuple(sorted(set(targets.get(relative, [])))),
        ))
    return sorted(entries, key=lambda entry: entry.corpus_id)


def find_corpus(root: Path, corpus_id: str) -> CorpusEntry | None:
    return next((entry for entry in corpus_catalog(root) if entry.corpus_id == corpus_id), None)
