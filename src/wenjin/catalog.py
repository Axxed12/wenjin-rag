"""语料目录：回答"向量库里现在有哪些东西"。

只读 manifest 和索引配置，不碰 Qdrant、不碰 MCP。检索服务、CLI 和 MCP 工具共用同一份
目录结果，避免各自实现一套"有哪些语料"的逻辑。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from wenjin.corpus_identity import CorpusIdentityError, resolve_identity, validate_identifier
from wenjin.manifest import LEGACY_SCOPE_FIELD, RETRIEVAL_SCOPE_FIELD, scope_files

PUBLIC_DIR = "corpora"
INTERNAL_DIR = "corpora-private"
# 索引目标配置同样分两处：公开语料的可提交，内部语料的必须忽略。
# 配置里有 corpus_id（含内部库名和版本）和 manifest 路径，`corpora-private/` 被忽略
# 而配置被提交，等于从另一条路把内部命名泄露出去。
PUBLIC_INDEX_DIR = "configs/indexes"
INTERNAL_INDEX_DIR = "configs/indexes-private"

# 一个目录为什么被隔离。刻意保持少量且稳定，运维按码定位，不用去猜错误文案。
PROBLEM_DIRECTORY_NAME = "directory_name_invalid"   # 目录名本身不合法（中文、空格等）
PROBLEM_DECLARED_ID = "declared_id_invalid"         # manifest 声明的 corpus_id 不合法
PROBLEM_IDENTITY_CONFLICT = "identity_conflict"     # 目录名与 manifest 声明不一致
PROBLEM_DUPLICATE_ID = "duplicate_corpus_id"        # 两份目录声明同一个身份
PROBLEM_MANIFEST_UNREADABLE = "manifest_unreadable"  # 读不出或解析不了
PROBLEM_MANIFEST_INVALID = "manifest_invalid"       # 结构不合法


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


@dataclass(frozen=True)
class CorpusProblem:
    """一个不能当作正常语料服务的目录，以及足够定位它的信息。

    刻意**不**把可疑 manifest 的内容当语料元数据带出来：这份语料的身份都还没确定，
    按它的声明值汇报等于让一个身份可疑的目录借目录接口露脸。

    这里**没有** `to_dict` 之类的序列化入口，是刻意的：`directory`/`location`/
    `declared_id` 都是原始目录名与路径，含 `corpora-private/` 底下的。一旦有网络接口
    原样输出它们，任何客户端（哪怕一个语料都读不了）都能读到"内网有一份被隔离的目录
    叫什么"。被隔离的目录按定义不可服务，调用方对它没有任何可执行动作，能看它的只有
    运维面——本地 CLI（`scripts/manage_access.py --list`）与服务端日志。
    """

    directory: str          # 磁盘目录名，按原样保留（可能就是非法字符）
    location: str           # 相对项目根的 manifest 路径
    visibility: str
    reason: str
    detail: str
    # 这份 manifest 自称的 corpus_id（非法或不一致的那个）。用于运维定位。
    # **不**参与 `find_corpus` 判定——采信声明值正是 P1-1 要防的事。
    declared_id: str | None = None


@dataclass(frozen=True)
class CorpusCatalog:
    """一次目录扫描的结果：能正常服务的条目 + 被隔离的问题目录。

    分成两半而不是"抛错或全好"，是为了限定**故障影响范围**：一个身份不合法的语料
    只让它自己被隔离，其余语料照常列出、检索和取件。隔离不等于放松——问题目录
    永远不进 `entries`，因此既不会被当成可查语料列出，也不会被 `find_corpus` 命中。

    `problems` 只给运维面用（本地CLI、服务端日志），**不要**接到网络接口的返回值上：
    见 `CorpusProblem` 的说明。
    """

    entries: tuple[CorpusEntry, ...]
    problems: tuple[CorpusProblem, ...]


def _problem(*, directory: str, visibility: str, manifest_path: Path, root: Path,
             reason: str, detail: str, declared_id: str | None = None) -> CorpusProblem:
    return CorpusProblem(
        directory=directory,
        location=manifest_path.relative_to(root).as_posix(),
        visibility=visibility,
        reason=reason,
        detail=detail,
        declared_id=declared_id,
    )


# 身份异常的原因码 -> 可以安全报给客户端的短说明（不含任何路径）。
_IDENTITY_DETAIL = {
    PROBLEM_DIRECTORY_NAME: "语料目录名含不允许的字符（只允许字母数字与 . _ -，且不以符号开头）",
    PROBLEM_DECLARED_ID: "manifest 声明的 corpus_id 含不允许的字符",
    PROBLEM_IDENTITY_CONFLICT: "语料目录名与 manifest 声明的 corpus_id 不一致",
}


def _identity_reason(directory: str, declared: str | None) -> str:
    """给身份异常分类，便于运维按原因码定位。

    只有 `resolve_identity` 已经拒绝之后才会问这里，所以三个分支覆盖它的全部拒绝理由。
    """
    for value, field_name, reason in (
        (directory, "语料目录名", PROBLEM_DIRECTORY_NAME),
        (declared, "corpus_id", PROBLEM_DECLARED_ID),
    ):
        if not value or not str(value).strip():
            continue
        try:
            validate_identifier(value, field=field_name)
        except CorpusIdentityError:
            return reason
    return PROBLEM_IDENTITY_CONFLICT


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


def corpus_catalog_report(root: Path) -> CorpusCatalog:
    """列出全部语料，按corpus_id排序；索引缺失不影响列出。

    身份取自**目录名与manifest声明的一致结果**，不是直接相信manifest：目录名和声明不一致
    时拒绝这一份（fail closed）。授权和取件都以corpus_id为准，如果这里信任声明值，
    一个放在`corpora-private/private-1/`却声明成`public-1`的语料就会顶着公开身份被列出，
    进而被公开策略放行。

    同一个corpus_id出现两次也拒绝，不取第一个匹配项——否则请求落到哪一份取决于遍历顺序。

    与"整份列表一起拒绝"的旧实现相比，这里只把**出问题的那一份**隔离进 `problems`：
    一个目录身份不合法不再让整个语料目录功能失效（旧行为下任意一个坏目录会让所有
    `/files/...` 取件返回503）。隔离的作用范围到此为止——问题目录不进 `entries`，
    所以它既不会被列为可查语料，也不会被取件命中。
    """
    targets = _index_targets_by_manifest(root)
    entries: list[CorpusEntry] = []
    problems: list[CorpusProblem] = []
    # corpus_id -> 已经收下的那一份目录（用于发现重复时把两者一起隔离）
    owners: dict[str, tuple[CorpusEntry, str, Path]] = {}
    # 已经判为"多份目录争同一个身份"的 corpus_id。不能只用 `in owners` 判定：
    # 发现重复时会 pop，pop 之后字典里没有它了，**第三份**同名目录就会被当成正主收下。
    # 那份目录一旦被服务，攻击者只要凑够三份同名目录，他的那份就能顶着该身份被授权放行。
    # 当前 _manifest_paths 只扫两个固定父目录下的一层，同名目录最多两份，所以还走不到；
    # 但那是**另一个函数**的属性，这里的正确性不能寄托在它身上。
    quarantined: set[str] = set()

    for manifest_path, visibility in _manifest_paths(root):
        directory = manifest_path.parent.name
        try:
            manifest = load_manifest(manifest_path)
        except (OSError, UnicodeDecodeError):
            problems.append(_problem(
                directory=directory, visibility=visibility, manifest_path=manifest_path,
                root=root, reason=PROBLEM_MANIFEST_UNREADABLE,
                detail="manifest 无法读取",
            ))
            continue
        except ValueError as error:
            # JSONDecodeError 也是 ValueError：解析不了归"读不出"，结构不对归"不合法"。
            unreadable = isinstance(error, json.JSONDecodeError)
            problems.append(_problem(
                directory=directory, visibility=visibility, manifest_path=manifest_path,
                root=root,
                reason=PROBLEM_MANIFEST_UNREADABLE if unreadable
                else PROBLEM_MANIFEST_INVALID,
                detail="manifest 无法解析为JSON" if unreadable
                else "manifest 结构不合法（需为对象且含 files 数组）",
            ))
            continue

        declared = manifest.get("corpus_id")
        if declared is not None and not isinstance(declared, str):
            # 非字符串的corpus_id以前会让resolve_identity在.strip()上抛AttributeError，
            # 那不是ValueError，取件端点会当成500——又一个"一个坏manifest打穿全部"。
            problems.append(_problem(
                directory=directory, visibility=visibility, manifest_path=manifest_path,
                root=root, reason=PROBLEM_DECLARED_ID,
                detail="manifest 声明的 corpus_id 不是字符串",
            ))
            continue

        try:
            identity = resolve_identity(
                directory=directory,
                declared=declared,
                context=f"（{manifest_path}）",
            )
        except CorpusIdentityError:
            reason = _identity_reason(directory, declared)
            declared_id = declared.strip() if declared and declared.strip() else None
            problems.append(_problem(
                directory=directory, visibility=visibility, manifest_path=manifest_path,
                root=root, reason=reason,
                detail=_IDENTITY_DETAIL[reason],
                declared_id=declared_id,
            ))
            continue

        if identity.corpus_id in quarantined:
            # 这个身份已经因为"多份目录争它"被隔离了。后面再冒出来的同名目录同样是
            # 可疑的一份，绝不能因为 owners 里已经空了就被收下。
            problems.append(_problem(
                directory=directory, visibility=visibility, manifest_path=manifest_path,
                root=root, reason=PROBLEM_DUPLICATE_ID,
                detail=f"corpus_id {identity.corpus_id} 由多个目录声明，无法确定以哪一份为准",
                declared_id=identity.corpus_id,
            ))
            continue

        if identity.corpus_id in owners:
            # 两份目录声明同一个身份：**两份都**隔离，并把已经收下的那份撤回来——
            # 无法判断哪一份才是正主，留一份在列表里等于让"哪份被服务"取决于遍历顺序。
            first_entry, first_directory, first_path = owners.pop(identity.corpus_id)
            entries.remove(first_entry)
            quarantined.add(identity.corpus_id)
            for where, where_visibility, where_path in (
                (first_directory, first_entry.visibility, first_path),
                (directory, visibility, manifest_path),
            ):
                problems.append(_problem(
                    directory=where, visibility=where_visibility,
                    manifest_path=where_path, root=root, reason=PROBLEM_DUPLICATE_ID,
                    detail=f"corpus_id {identity.corpus_id} 由多个目录声明，无法确定以哪一份为准",
                    declared_id=identity.corpus_id,
                ))
            continue

        relative = manifest_path.relative_to(root).as_posix()
        files = manifest.get("files", [])
        # 范围白名单按该语料自己的语义取，不要在这里重写一份判断。
        in_scope = tuple(scope_files(manifest))
        uses_retrieval = any(RETRIEVAL_SCOPE_FIELD in item for item in files)
        entry = CorpusEntry(
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
        )
        entries.append(entry)
        owners[identity.corpus_id] = (entry, directory, manifest_path)

    return CorpusCatalog(
        entries=tuple(sorted(entries, key=lambda entry: entry.corpus_id)),
        problems=tuple(problems),
    )


def corpus_catalog(root: Path) -> list[CorpusEntry]:
    """只返回能正常服务的语料，保持既有调用方的签名不变。

    需要知道"有哪些目录被隔离、为什么"的地方必须用 `corpus_catalog_report`，
    否则问题会被静默吞掉。
    """
    return list(corpus_catalog_report(root).entries)


def find_corpus(root: Path, corpus_id: str) -> CorpusEntry | None:
    """按corpus_id取一份语料；只有**请求的这个**身份有问题时才失败。

    旧实现扫全馆并对任一坏目录抛出，于是一个不相干的目录能让所有语料的取件变成503。
    现在只在请求的 id 正好是某个被隔离的**目录名**时拒绝，其余照常返回——既不放松
    （问题语料永远取不到），也不殃及（正常语料不受影响）。

    判定只看目录名，不看问题 manifest 自称的 `corpus_id`：采信那个声明值正是 P1-1
    要防的事。所以一个坏目录在 manifest 里自称 `osg-3.6.5`，不会让真正名为
    `osg-3.6.5` 的那份语料停止服务；它自己也仍然取不到（它不在 `entries` 里）。

    请求一个既无目录、也无问题目录占用的 id 仍然返回 `None`，由调用方按"不存在"处理。
    """
    report = corpus_catalog_report(root)
    for problem in report.problems:
        if problem.directory == corpus_id:
            raise CorpusIdentityError(
                f"语料 {corpus_id} 的身份无法确定（{problem.reason}）：{problem.detail}"
                f"（{problem.location}）"
            )
    return next(
        (entry for entry in report.entries if entry.corpus_id == corpus_id), None
    )

