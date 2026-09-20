"""manifest 的统一契约：路径、标识符、检索范围与内容指纹。

启动校验、候选验收和切换前检查必须用**同一套**判定。之前这几处各写了一份，强弱不一：
`multiformat` 不校验 `raw_data_subdir` 是否落在可信根内，`validate_target` 直接相信
manifest 自己声明的指纹。本模块把这些判定收敛到一处。

两条范围语义**必须并存**，不能为了校验方便改掉旧定义：
- legacy OSG manifest 用 `indexable`；
- 五个新语料用 `retrieval_eligible`。
指纹算法沿用既有实现（见 `retrieval_fingerprint`），改算法会让已验收的指纹全部失配。
"""

import json
from pathlib import Path

from wenjin.content_hashing import (
    BINARY_IDENTITY_V1,
    manifest_content_fingerprint,
    sha256_hex,
)
from wenjin.corpus_identity import (
    CorpusIdentityError,
    resolve_identity,
    validate_identifier,
)
from wenjin.paths import UnsafePathError, resolve_within, validate_relative_path

# 检索范围的两种字段语义。新manifest用后者，legacy OSG用前者。
LEGACY_SCOPE_FIELD = "indexable"
RETRIEVAL_SCOPE_FIELD = "retrieval_eligible"


class ManifestError(ValueError):
    """manifest 不满足契约：路径越界、标识符非法、记录缺字段或指纹不符。"""


def load(manifest_path: Path) -> dict:
    path = Path(manifest_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as error:
        raise ManifestError(f"manifest无法解析：{path}") from error
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise ManifestError(f"manifest结构无效：{path}")
    return data


def trusted_raw_root(manifest: dict, raw_data_root: Path) -> Path:
    """把 `raw_data_subdir` 解析到可信 raw_data 根之内。

    必须先做这一步，再逐条校验文件路径：subdir 本身可能是 `../..`，
    那样"每个文件都在 raw_root 内"这个检查就失去意义了。
    """
    subdir = str(manifest.get("raw_data_subdir") or "")
    if not subdir:
        raise ManifestError("manifest缺少raw_data_subdir")
    try:
        resolved = resolve_within(Path(raw_data_root), subdir)
    except UnsafePathError as error:
        raise ManifestError(f"raw_data_subdir越界或非法：{subdir}") from error
    return resolved


def validate_identity(manifest: dict, *, manifest_path: Path) -> str:
    """校验 library/version/corpus_id 的标识符规则，并返回一致的 corpus_id。

    目录名、manifest 声明之间不一致时拒绝（与索引目标、取件共用同一判定）。
    """
    library = validate_identifier(str(manifest.get("library") or ""), field="library")
    version = validate_identifier(str(manifest.get("version") or ""), field="version")
    identity = resolve_identity(
        directory=Path(manifest_path).parent.name,
        declared=manifest.get("corpus_id"),
        context=f"（{manifest_path}）",
    )
    return f"{identity.corpus_id}|{library}|{version}"


def file_records(manifest: dict) -> list[dict]:
    """逐条校验文件记录：路径必须是语料内相对路径且不越界，且必须有合法内容Hash。

    缺 Hash 的记录会让"取回的是否为被引用的原文"无法证明，因此直接拒绝整份 manifest。
    """
    records = []
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or not entry.get("path"):
            raise ManifestError("文件记录缺少path")
        try:
            relative = validate_relative_path(str(entry["path"]), field="文件路径")
        except UnsafePathError as error:
            raise ManifestError(f"文件路径越界或非法：{entry.get('path')}") from error
        content_hash = ""
        for field in ("raw_sha256", "sha256"):
            value = entry.get(field)
            if value:
                content_hash = str(value).strip().lower()
                break
        if not content_hash:
            raise ManifestError(f"文件记录缺少内容Hash：{relative}")
        if len(content_hash) != 64 or any(c not in "0123456789abcdef" for c in content_hash):
            raise ManifestError(f"文件记录的内容Hash格式非法：{relative}")
        records.append({**entry, "path": relative, "_content_hash": content_hash})
    paths = [record["path"] for record in records]
    if len(set(paths)) != len(paths):
        duplicates = sorted({p for p in paths if paths.count(p) > 1})
        raise ManifestError(f"manifest重复登记了同一路径，来源映射不再唯一：{duplicates}")
    return records


def uses_retrieval_scope(manifest: dict) -> bool:
    """是否有新语料的 `retrieval_eligible` 语义。"""
    return any(RETRIEVAL_SCOPE_FIELD in entry for entry in manifest.get("files", []))


def scope_files(manifest: dict) -> list[str]:
    """按对应语义返回检索范围白名单。两种语义各自保持原样。"""
    if uses_retrieval_scope(manifest):
        return [
            str(entry["path"])
            for entry in manifest["files"]
            if entry.get(RETRIEVAL_SCOPE_FIELD)
        ]
    return [
        str(entry["path"])
        for entry in manifest["files"]
        if entry.get(LEGACY_SCOPE_FIELD) is not False
    ]


def retrieval_fingerprint(manifest: dict) -> str:
    """**重算**检索范围的内容指纹，不读取 manifest 自己声明的值。

    算法必须与既有实现逐字节一致：新语料是把 `retrieval_eligible` 映射到 `indexable`
    后套用同一套指纹函数（见 `prepare_corpus_import.py`）。改算法会让已验收的
    `retrieval_content_sha256` 与目标配置里的 `canonical_corpus_sha256` 全部失配。
    """
    if uses_retrieval_scope(manifest):
        remapped = {
            "files": [
                {**entry, LEGACY_SCOPE_FIELD: bool(entry.get(RETRIEVAL_SCOPE_FIELD))}
                for entry in manifest["files"]
            ]
        }
        return manifest_content_fingerprint(remapped)
    return manifest_content_fingerprint(manifest)


def canonical_fingerprint(manifest: dict) -> str:
    """全语料的内容指纹，同样由记录重算。"""
    return manifest_content_fingerprint(manifest)


def manifest_sha256(manifest_path: Path) -> str:
    """manifest 文件自身的字节哈希，用于把索引绑定到具体一份清单。"""
    return sha256_hex(Path(manifest_path).read_bytes())


def validate_manifest(
    manifest_path: Path,
    *,
    raw_data_root: Path | None = None,
    check_identity: bool = True,
) -> dict:
    """一次跑完全部**结构性**校验；调用方不必各自记住该查哪几项。

    给了 `raw_data_root` 就同时校验 `raw_data_subdir` 落在可信根内——
    "导入时拒绝、验收时接受"是不允许的，两侧必须用同一套判断。
    """
    manifest = load(manifest_path)
    if check_identity:
        validate_identity(manifest, manifest_path=manifest_path)
    file_records(manifest)
    if raw_data_root is not None:
        trusted_raw_root(manifest, raw_data_root)
    return manifest


def read_verified_bytes(entry: dict, raw_root: Path) -> bytes:
    """读取一条记录对应的文件字节，并校验它与记录里的内容Hash一致。"""
    relative = str(entry["path"]).replace("\\", "/")
    try:
        path = resolve_within(raw_root, relative)
    except UnsafePathError as error:
        raise ManifestError(f"文件路径越界：{relative}") from error
    if not path.is_file():
        raise ManifestError(f"文件不存在：{relative}")
    content = path.read_bytes()
    expected = entry.get("_content_hash") or entry.get("raw_sha256") or entry.get("sha256")
    if sha256_hex(content) != str(expected).strip().lower():
        raise ManifestError(f"文件字节与登记Hash不符：{relative}")
    return content


__all__ = [
    "BINARY_IDENTITY_V1",
    "CorpusIdentityError",
    "LEGACY_SCOPE_FIELD",
    "ManifestError",
    "RETRIEVAL_SCOPE_FIELD",
    "canonical_fingerprint",
    "file_records",
    "load",
    "manifest_sha256",
    "read_verified_bytes",
    "retrieval_fingerprint",
    "scope_files",
    "trusted_raw_root",
    "uses_retrieval_scope",
    "validate_identity",
    "validate_manifest",
]
