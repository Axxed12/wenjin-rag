"""语料目录：回答"向量库里现在有哪些东西"。

只读 manifest 和索引配置，不碰 Qdrant、不碰 MCP。检索服务、CLI 和 MCP 工具共用同一份
目录结果，避免各自实现一套"有哪些语料"的逻辑。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from wenjin.corpus_identity import CorpusIdentityError, resolve_identity
from wenjin.manifest import LEGACY_SCOPE_FIELD, RETRIEVAL_SCOPE_FIELD, scope_files

PUBLIC_DIR = "corpora"
INTERNAL_DIR = "corpora-private"
# 索引目标配置同样分两处：公开语料的可提交，内部语料的必须忽略。
# 配置里有 corpus_id（含内部库名和版本）和 manifest 路径，`corpora-private/` 被忽略
# 而配置被提交，等于从另一条路把内部命名泄露出去。
PUBLIC_INDEX_DIR = "configs/indexes"
INTERNAL_INDEX_DIR = "configs/indexes-private"


def index_config_dir(root: Path, *, visibility: str) -> Path:
    """按语料可见性选择索引配置的存放目录。"""
    return root / (PUBLIC_INDEX_DIR if visibility == "public" else INTERNAL_INDEX_DIR)


def index_config_dirs(root: Path) -> tuple[Path, ...]:
    """索引配置的两处存放位置；公开在前，便于报告顺序稳定。"""
    return (root / PUBLIC_INDEX_DIR, root / INTERNAL_INDEX_DIR)


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
    # 这份语料用的是哪套检索范围语义：新语料是 `retrieval_eligible`，
    # legacy OSG 是 `indexable`。两者计数口径不同，必须一起报出来。
    scope_field: str = RETRIEVAL_SCOPE_FIELD

    @property
    def indexed(self) -> bool:
        """**只表示有索引配置提到这份 manifest**，不表示那份索引可用。

        要判断能不能真的检索，必须另外做检查（见 `list_corpora` 的 index_state）。
        """
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
            # 按**该语料自己的语义**统计：OSG 用 `indexable`，新语料用
            # `retrieval_eligible`。只数 retrieval_eligible 会把 OSG 报成 0 个可检索文件。
            "retrieval_eligible_files": self.retrieval_eligible_files,
            "scope_field": self.scope_field,
            "has_index_config": self.indexed,
            "indexed": self.indexed,
            "index_targets": list(self.index_targets),
        }
        if include_files:
            payload["retrieval_scope_files"] = list(self.retrieval_scope_files)
        return payload


def load_manifest(manifest_path: Path) -> dict:
    """读取并做最小结构校验；多个模块共用同一个入口，避免各写一份读法。"""
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise ValueError(f"manifest结构无效：{manifest_path}")
    return data


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


def _iter_index_target_configs(root: Path):
    """遍历公开与内部两处索引配置里的索引目标原文。

    单份配置和路由清单（带 `candidates`）都算。只读 JSON，不构造 `IndexTarget`，
    这样目录模块不需要依赖索引注册表。
    """
    for config_dir in index_config_dirs(root):
        if not config_dir.is_dir():
            continue
        for path in sorted(config_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError):
                continue
            for item in (data.get("candidates") or [data]):
                if not isinstance(item, dict) or "manifest_path" not in item:
                    continue
                yield item.get("target_id", path.stem), item


def _index_targets_by_manifest(root: Path) -> dict[str, list[str]]:
    """从索引配置里反查每个manifest已经建了哪些target。"""
    mapping: dict[str, list[str]] = {}
    for target_id, item in _iter_index_target_configs(root):
        mapping.setdefault(item["manifest_path"], []).append(target_id)
    return mapping


def index_target_configs(root: Path) -> dict[str, dict]:
    """按 target_id 取索引配置原文。

    同一个 target 会同时出现在单份配置和路由清单里，这是正常的，内容相同就去重。
    但**内容冲突必须报错**：静默取第一份会让"这个 target 到底指向哪个 collection"
    取决于文件遍历顺序，而调用方完全看不出来。
    """
    configs: dict[str, dict] = {}
    for target_id, item in _iter_index_target_configs(root):
        existing = configs.get(target_id)
        if existing is None:
            configs[target_id] = item
        elif existing != item:
            raise ValueError(
                f"target_id {target_id} 在多份索引配置里内容冲突，无法确定以哪份为准"
            )
    return configs


def corpus_catalog(root: Path) -> list[CorpusEntry]:
    """列出全部语料，按corpus_id排序；索引缺失不影响列出。

    身份取自**目录名与manifest声明的一致结果**，不是直接相信manifest：目录名和声明不一致
    时拒绝整份列表（fail closed）。授权和取件都以corpus_id为准，如果这里信任声明值，
    一个放在`corpora-private/private-1/`却声明成`public-1`的语料就会顶着公开身份被列出，
    进而被公开策略放行。

    同一个corpus_id出现两次也拒绝，不取第一个匹配项——否则请求落到哪一份取决于遍历顺序。
    """
    targets = _index_targets_by_manifest(root)
    entries = []
    owners: dict[str, Path] = {}
    for manifest_path, visibility in _manifest_paths(root):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        identity = resolve_identity(
            directory=manifest_path.parent.name,
            declared=manifest.get("corpus_id"),
            context=f"（{manifest_path}）",
        )
        if identity.corpus_id in owners:
            raise CorpusIdentityError(
                f"corpus_id重复：{identity.corpus_id} 同时由 "
                f"{owners[identity.corpus_id]} 和 {manifest_path} 声明"
            )
        owners[identity.corpus_id] = manifest_path
        relative = manifest_path.relative_to(root).as_posix()
        files = manifest.get("files", [])
        # 范围白名单按该语料自己的语义取，不要在这里重写一份判断。
        in_scope = tuple(scope_files(manifest))
        uses_retrieval = any(RETRIEVAL_SCOPE_FIELD in item for item in files)
        entries.append(CorpusEntry(
            corpus_id=identity.corpus_id,
            library=manifest.get("library", "unknown"),
            version=manifest.get("version", "unknown"),
            visibility=visibility,
            license=manifest.get("license", "unknown"),
            source_url=manifest.get("source_url"),
            raw_data_subdir=manifest.get("raw_data_subdir", ""),
            files=len(files),
            retrieval_eligible_files=len(in_scope),
            retrieval_scope_files=in_scope,
            manifest_path=relative,
            index_targets=tuple(sorted(set(targets.get(relative, [])))),
            scope_field=RETRIEVAL_SCOPE_FIELD if uses_retrieval else LEGACY_SCOPE_FIELD,
        ))
    return sorted(entries, key=lambda entry: entry.corpus_id)


def find_corpus(root: Path, corpus_id: str) -> CorpusEntry | None:
    return next((entry for entry in corpus_catalog(root) if entry.corpus_id == corpus_id), None)
