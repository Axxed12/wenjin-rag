"""版本化索引注册表：验证候选后原子切换，保留旧目标用于回退。"""

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from wenjin.corpus_identity import (  # noqa: E402
    CorpusIdentity,
    CorpusIdentityError,
    resolve_identity,
)

COLLECTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True)
class IndexTarget:
    target_id: str
    library: str
    version: str
    strategy: str
    dense_collection: str
    bm25_path: str
    manifest_path: str
    manifest_sha256: str
    chunk_count: int
    vector_dimension: int
    embedding_model: str
    canonical_corpus_sha256: str | None = None
    normalization_profile: str | None = None
    # 语料身份显式记录，避免多个模块各自从manifest路径推导目录名这一隐式约定。
    # 旧配置没有这个字段，回退到"manifest所在目录名"以保持兼容。
    corpus_id: str | None = None
    # 发布快照中的原文可与当前raw_data隔离；None保持所有旧target的路径语义。
    # manifest.raw_data_subdir与chunk.source_path不变，只替换其所属的raw根目录。
    raw_snapshot_root: str | None = None

    def corpus_identity(self) -> str:
        return self.corpus_id or Path(self.manifest_path).parent.name

    @classmethod
    def from_dict(cls, data: dict) -> "IndexTarget":
        target = cls(**data)
        if not target.target_id or not target.library or not target.version:
            raise ValueError("target_id、library和version不能为空")
        if not COLLECTION_NAME.fullmatch(target.dense_collection):
            raise ValueError("dense_collection名称不安全")
        if target.chunk_count < 1 or target.vector_dimension < 1:
            raise ValueError("chunk_count和vector_dimension必须大于0")
        if not re.fullmatch(r"[0-9a-f]{64}", target.manifest_sha256):
            raise ValueError("manifest_sha256必须是小写SHA256")
        if target.canonical_corpus_sha256 and not re.fullmatch(
            r"[0-9a-f]{64}", target.canonical_corpus_sha256
        ):
            raise ValueError("canonical_corpus_sha256必须是小写SHA256")
        if target.raw_snapshot_root is not None:
            if not target.raw_snapshot_root or Path(target.raw_snapshot_root).is_absolute():
                raise ValueError("raw_snapshot_root必须是项目内相对路径")
        return target


def resolve_project_path(root: Path, relative: str) -> Path:
    """索引和manifest必须使用项目内相对路径，禁止目录逃逸。"""
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ValueError(f"注册表路径必须为相对路径：{relative}")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"注册表路径超出项目目录：{relative}")
    return resolved


def target_raw_data_root(root: Path, target: IndexTarget) -> Path:
    """返回target绑定的可信原文根；旧target继续使用项目`raw_data/`。"""
    if target.raw_snapshot_root is None:
        return (root / "raw_data").resolve()
    return resolve_project_path(root, target.raw_snapshot_root)


def resolve_corpus_identity(target: IndexTarget, *, root: Path) -> CorpusIdentity:
    """确定索引目标对应的语料身份，三个来源冲突时拒绝。

    这是 `wenjin.corpus_identity.resolve_identity` 在索引场景下的包装：多了
    「读manifest取声明值」这一步。语料目录和原文取件用的是同一个底层判定，
    因此三处入口不会出现强弱不一致的身份校验。
    """
    manifest_path = resolve_project_path(root, target.manifest_path)
    declared = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as error:
            raise CorpusIdentityError(
                f"manifest无法解析，无法确定语料身份：{target.manifest_path}"
            ) from error
        declared = manifest.get("corpus_id")
    return resolve_identity(
        directory=manifest_path.parent.name,
        declared=declared,
        explicit=target.corpus_id,
        context=f"（索引目标 {target.target_id}）",
    )


def load_target(path: Path) -> IndexTarget:
    return IndexTarget.from_dict(json.loads(path.read_text(encoding="utf-8-sig")))


def validate_target(target: IndexTarget, *, root: Path, qdrant_client) -> dict:
    """校验manifest、BM25和Qdrant指向同一规模/版本的完整索引。"""
    manifest_path = resolve_project_path(root, target.manifest_path)
    bm25_path = resolve_project_path(root, target.bm25_path)
    if not manifest_path.is_file() or not bm25_path.is_file():
        raise FileNotFoundError("manifest或BM25索引不存在")
    actual_manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if actual_manifest_hash != target.manifest_sha256:
        raise ValueError("manifest SHA256与目标配置不一致")
    from wenjin import manifest as manifest_module

    manifest = manifest_module.load(manifest_path)
    # manifest本身也要过契约：标识符、路径越界、每条记录的内容Hash。
    manifest_module.validate_identity(manifest, manifest_path=manifest_path)
    manifest_module.file_records(manifest)
    if target.canonical_corpus_sha256:
        # **按记录重算**，不能直接相信manifest声明的值：声明的字段可以被改，
        # 而重算必须真的读过每一条记录的双Hash才可能对上。
        recomputed = manifest_module.retrieval_fingerprint(manifest)
        if recomputed != target.canonical_corpus_sha256:
            raise ValueError("canonical语料指纹与目标配置不一致（按记录重算）")
        declared = manifest.get("retrieval_content_sha256")
        if declared and declared != recomputed:
            raise ValueError("manifest声明的检索指纹与按记录重算的结果不一致")
    # Connection上下文只负责提交/回滚，不会关闭SQLite文件句柄；Windows删除或重建
    # candidate BM25时需要明确释放句柄。
    with closing(sqlite3.connect(bm25_path)) as database:
        bm25_count = database.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        versions = {row[0] for row in database.execute("SELECT DISTINCT version FROM chunks")}
        strategies = {row[0] for row in database.execute("SELECT DISTINCT strategy FROM chunks")}
        bm25_metadata_rows = None
        if target.normalization_profile:
            columns = {
                row[1] for row in database.execute("PRAGMA table_info(chunks)")
            }
            required = {
                "content_sha256",
                "source_raw_sha256",
                "source_canonical_sha256",
                "normalization",
            }
            if not required.issubset(columns):
                raise ValueError("BM25索引缺少双Hash元数据列")
            profile_clause = (
                "normalization IS NOT NULL"
                if target.normalization_profile == "mixed-versioned-v1"
                else "normalization = ?"
            )
            params = () if target.normalization_profile == "mixed-versioned-v1" else (
                target.normalization_profile,
            )
            bm25_metadata_rows = database.execute(
                f"""SELECT COUNT(*) FROM chunks
                   WHERE {profile_clause}
                     AND length(content_sha256) = 64
                     AND length(source_raw_sha256) = 64
                     AND length(source_canonical_sha256) = 64""",
                params,
            ).fetchone()[0]
    if bm25_count != target.chunk_count:
        raise ValueError(f"BM25行数{bm25_count}与期望{target.chunk_count}不一致")
    if versions != {target.version} or strategies != {target.strategy}:
        raise ValueError("BM25版本或切片策略与目标配置不一致")

    collection = qdrant_client.get_collection(target.dense_collection)
    vector_config = collection.config.params.vectors
    vector_size = getattr(vector_config, "size", None)
    status = getattr(collection.status, "value", str(collection.status))
    if collection.points_count != target.chunk_count:
        raise ValueError(
            f"Qdrant points{collection.points_count}与期望{target.chunk_count}不一致"
        )
    if vector_size != target.vector_dimension:
        raise ValueError(f"向量维度{vector_size}与期望{target.vector_dimension}不一致")
    if str(status).lower() != "green":
        raise ValueError(f"Qdrant collection状态不是green：{status}")
    qdrant_metadata_points = None
    if target.normalization_profile:
        from qdrant_client import models

        match = (
            models.MatchAny(any=["text-newline-lf-v1", "binary-identity-v1"])
            if target.normalization_profile == "mixed-versioned-v1"
            else models.MatchValue(value=target.normalization_profile)
        )
        count_filter = models.Filter(must=[models.FieldCondition(
                key="normalization",
                match=match,
            )])
        qdrant_metadata_points = qdrant_client.count(
            target.dense_collection, count_filter=count_filter, exact=True
        ).count
        if bm25_metadata_rows != target.chunk_count:
            raise ValueError("BM25双Hash元数据不完整")
        if qdrant_metadata_points != target.chunk_count:
            raise ValueError("Qdrant双Hash元数据不完整")
    return {
        "target_id": target.target_id,
        "manifest_sha256": actual_manifest_hash,
        "canonical_corpus_sha256": target.canonical_corpus_sha256,
        "bm25_rows": bm25_count,
        "qdrant_points": collection.points_count,
        "bm25_metadata_rows": bm25_metadata_rows,
        "qdrant_metadata_points": qdrant_metadata_points,
        "vector_dimension": vector_size,
        "collection_status": status,
        "valid": True,
    }


class IndexRegistryStore:
    """单管理员进程使用的原子JSON注册表；运行中MCP在重启时加载新active。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict:
        data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if data.get("schema_version") != 1 or not isinstance(data.get("revision"), int):
            raise ValueError("索引注册表schema或revision无效")
        IndexTarget.from_dict(data["active"])
        for item in data.get("history", []):
            IndexTarget.from_dict(item)
        return data

    def active(self) -> IndexTarget:
        return IndexTarget.from_dict(self.load()["active"])

    def previous(self) -> IndexTarget:
        data = self.load()
        if not data.get("history"):
            raise ValueError("没有可回退的历史索引")
        return IndexTarget.from_dict(data["history"][-1])

    def initialize(self, target: IndexTarget, *, reason: str) -> dict:
        if self.path.exists():
            raise FileExistsError(f"索引注册表已存在：{self.path}")
        data = {
            "schema_version": 1,
            "revision": 1,
            "active": asdict(target),
            "history": [],
            "events": [self._event("initialize", None, target, reason)],
        }
        self._atomic_write(data)
        return data

    def switch(self, target: IndexTarget, *, reason: str) -> dict:
        data = self.load()
        current = IndexTarget.from_dict(data["active"])
        if current.target_id == target.target_id:
            raise ValueError("候选target已经是active")
        data["history"].append(asdict(current))
        data["active"] = asdict(target)
        data["revision"] += 1
        data.setdefault("events", []).append(self._event("switch", current, target, reason))
        self._atomic_write(data)
        return data

    def rollback(self, *, reason: str) -> dict:
        data = self.load()
        if not data.get("history"):
            raise ValueError("没有可回退的历史索引")
        current = IndexTarget.from_dict(data["active"])
        previous = IndexTarget.from_dict(data["history"].pop())
        data["active"] = asdict(previous)
        data["revision"] += 1
        data.setdefault("events", []).append(
            self._event("rollback", current, previous, reason)
        )
        self._atomic_write(data)
        return data

    @staticmethod
    def _event(action: str, previous: IndexTarget | None, target: IndexTarget, reason: str) -> dict:
        return {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "previous_target_id": previous.target_id if previous else None,
            "target_id": target.target_id,
            "reason": reason,
        }

    def _atomic_write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
