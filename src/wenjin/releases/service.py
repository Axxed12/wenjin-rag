"""从已准入候选封存不可变release，再以CAS切换active指针。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from qdrant_client import models

from wenjin import manifest as manifest_module
from wenjin.index_audit import admit_index
from wenjin.index_fingerprint import canonical_json, file_sha256, qdrant_fingerprint
from wenjin.index_registry import (
    IndexTarget,
    resolve_project_path,
    target_raw_data_root,
)
from wenjin.paths import resolve_within
from wenjin.releases.catalog import ReleaseCatalog, ReleaseConflictError
from wenjin.retrieval.source_context import (
    reviewed_profile_for_target,
    sealed_profile_for_target,
)
from wenjin.serving import CorpusSources

_LIBRARY_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_EXPECTED_MODEL_UNSET = object()


class ReleaseValidationError(RuntimeError):
    """release封存物与完整性报告不匹配，拒绝标ready/activate。"""

    code = "release_validation_failed"

    def __init__(self, release_id: str, detail: str) -> None:
        self.release_id = release_id
        super().__init__(detail)


class ReleaseService:
    """共享资料库发布服务；候选与active永不原地覆盖。"""

    def __init__(self, root: Path, catalog: ReleaseCatalog, qdrant_client) -> None:
        self.root = Path(root).resolve()
        self.catalog = catalog
        self.qdrant_client = qdrant_client

    def prepare(
        self,
        library_id: str,
        target: IndexTarget,
        *,
        reason: str = "",
        actor: str | None = None,
    ) -> dict:
        """从已准入候选复制source、BM25和Qdrant，生成ready但未active的release。

        所有写入路径使用新release ID并以独占创建；失败时只在catalog留下failed记录和
        owned_resources，不会删改调用者候选或任何active资产。
        """
        if not _LIBRARY_ID.fullmatch(library_id):
            raise ValueError("library_id格式无效")
        if _normalize_library(target.library) != library_id:
            raise ValueError("候选target不属于指定library_id")

        release_id = uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        release_rel = Path("data/releases") / library_id / release_id
        release_dir = resolve_project_path(self.root, release_rel.as_posix())
        raw_snapshot_rel = (release_rel / "raw_data").as_posix()
        source_manifest_path = resolve_project_path(self.root, target.manifest_path)
        release_manifest_rel = (
            release_rel / "corpora" / target.corpus_identity() / "source-manifest.json"
        ).as_posix()
        release_manifest_path = resolve_project_path(self.root, release_manifest_rel)
        qdrant_collection = f"rel_{release_id}"
        owned_resources = [
            release_rel.as_posix(),
            f"qdrant:{qdrant_collection}",
        ]
        record = {
            "release_id": release_id,
            "library_id": library_id,
            "status": "preparing",
            "source_target_id": target.target_id,
            "source_manifest_path": target.manifest_path,
            "target": None,
            "release_digest": None,
            "created_at": created_at,
            "reason": reason,
            "prepared_by": actor,
            "owned_resources": owned_resources,
            "validation": None,
        }
        self.catalog.register(record)

        try:
            # 先从候选逐chunk核验，随后复制；后面再对独占release重复准入。
            candidate_report = admit_index(
                target, root=self.root, qdrant_client=self.qdrant_client
            )
            source_target = target
            serving_profile = reviewed_profile_for_target(source_target)
            source_raw_root = target_raw_data_root(self.root, source_target)
            source_manifest_bytes = source_manifest_path.read_bytes()
            source_manifest_hash = hashlib.sha256(source_manifest_bytes).hexdigest()
            manifest = manifest_module.validate_manifest(
                source_manifest_path, raw_data_root=source_raw_root
            )
            sources = CorpusSources.from_manifest(
                source_manifest_path,
                raw_root=source_raw_root,
                expected_corpus_id=source_target.corpus_identity(),
            )

            release_dir.mkdir(parents=True, exist_ok=False)
            raw_snapshot_root = resolve_project_path(self.root, raw_snapshot_rel)
            raw_snapshot_root.mkdir(parents=True, exist_ok=False)
            release_manifest_path.parent.mkdir(parents=True, exist_ok=False)
            if file_sha256(source_manifest_path) != source_manifest_hash:
                raise ValueError("候选manifest在release复制期间发生变化")
            with release_manifest_path.open("xb") as output:
                output.write(source_manifest_bytes)
                output.flush()
                os.fsync(output.fileno())
            if (
                file_sha256(release_manifest_path) != source_manifest_hash
                or file_sha256(source_manifest_path) != source_manifest_hash
            ):
                raise ValueError("候选manifest复制期间发生变化或副本SHA不一致")

            source_hashes: dict[str, str] = {}
            raw_subdir = str(manifest.get("raw_data_subdir") or "")
            for entry in manifest_module.file_records(manifest):
                relative = entry["path"]
                source_path = sources.locate(relative)
                actual_hash = file_sha256(source_path)
                if actual_hash != entry["_content_hash"]:
                    raise ValueError(f"候选原文Hash与manifest不一致：{relative}")
                suffix = Path(raw_subdir) / Path(relative)
                destination = resolve_within(raw_snapshot_root, suffix.as_posix())
                destination.parent.mkdir(parents=True, exist_ok=True)
                _copy_file_exclusive(source_path, destination)
                if (
                    file_sha256(destination) != actual_hash
                    or file_sha256(source_path) != actual_hash
                ):
                    raise ValueError(f"release原文副本SHA不一致：{relative}")
                source_hashes[relative] = actual_hash

            source_bm25_path = resolve_project_path(self.root, source_target.bm25_path)
            source_bm25_sha = file_sha256(source_bm25_path)
            bm25_rel = (release_rel / "bm25.sqlite").as_posix()
            bm25_path = resolve_project_path(self.root, bm25_rel)
            _copy_file_exclusive(source_bm25_path, bm25_path)
            bm25_sha = file_sha256(bm25_path)
            if bm25_sha != source_bm25_sha or file_sha256(source_bm25_path) != source_bm25_sha:
                raise ValueError("候选BM25复制期间发生变化或副本SHA不一致")

            qdrant_sha = qdrant_fingerprint(
                self.qdrant_client, source_target.dense_collection
            )
            source_collection = self.qdrant_client.get_collection(
                source_target.dense_collection
            )
            create_kwargs = {
                "vectors_config": source_collection.config.params.vectors,
            }
            shard_number = getattr(source_collection.config.params, "shard_number", None)
            if shard_number is not None:
                create_kwargs["shard_number"] = shard_number
            on_disk_payload = getattr(source_collection.config.params, "on_disk_payload", None)
            if on_disk_payload is not None:
                create_kwargs["on_disk_payload"] = on_disk_payload
            if self.qdrant_client.collection_exists(qdrant_collection):
                raise FileExistsError(f"release collection已存在：{qdrant_collection}")
            self.qdrant_client.create_collection(qdrant_collection, **create_kwargs)
            offset = None
            while True:
                points, offset = self.qdrant_client.scroll(
                    source_target.dense_collection,
                    limit=128,
                    offset=offset,
                    with_payload=True,
                    with_vectors=True,
                )
                if points:
                    self.qdrant_client.upsert(
                        qdrant_collection,
                        points=[
                            models.PointStruct(
                                id=point.id,
                                payload=point.payload or {},
                                vector=point.vector,
                            )
                            for point in points
                        ],
                        wait=True,
                    )
                if offset is None:
                    break
            copied_qdrant_sha = qdrant_fingerprint(self.qdrant_client, qdrant_collection)
            if copied_qdrant_sha != qdrant_sha:
                raise ValueError("Qdrant release副本指纹与候选不一致")
            if qdrant_fingerprint(self.qdrant_client, source_target.dense_collection) != qdrant_sha:
                raise ValueError("候选Qdrant在release复制期间发生变化")

            release_target = IndexTarget.from_dict({
                **source_target.__dict__,
                "target_id": f"release-{release_id}",
                "dense_collection": qdrant_collection,
                "bm25_path": bm25_rel,
                "manifest_path": release_manifest_rel,
                "raw_snapshot_root": raw_snapshot_rel,
            })
            release_report = admit_index(
                release_target, root=self.root, qdrant_client=self.qdrant_client
            )
            source_fingerprint = _hash_mapping(source_hashes)
            release_digest = hashlib.sha256(canonical_json({
                "release_id": release_id,
                "library_id": library_id,
                "target": release_target.__dict__,
                "manifest_sha256": source_manifest_hash,
                "source_sha256": source_fingerprint,
                "bm25_sha256": bm25_sha,
                "qdrant_sha256": copied_qdrant_sha,
                "serving_profile": serving_profile,
            })).hexdigest()
            validation = {
                "valid": True,
                "candidate_admission": candidate_report,
                "release_admission": release_report,
                "source_files": len(source_hashes),
                "source_sha256": source_fingerprint,
                "manifest_sha256": source_manifest_hash,
                "bm25_sha256": bm25_sha,
                "qdrant_sha256": copied_qdrant_sha,
                "chunk_count": release_target.chunk_count,
            }
            return self.catalog.mark_ready(
                release_id,
                seal={
                    "target": release_target.__dict__,
                    "release_digest": release_digest,
                    "source_hashes": source_hashes,
                    "serving_profile": serving_profile,
                    "validation": validation,
                },
            )
        except Exception as error:
            # 只记录本次release拥有的路径/collection；不删除候选或部分副本，方便审计恢复。
            try:
                self.catalog.mark_failed(
                    release_id,
                    failure={"type": type(error).__name__, "detail": str(error)},
                )
            except Exception:
                pass
            raise

    def validate(self, release_id: str) -> dict:
        record = self.catalog.get_release(release_id)
        if record["status"] not in {"ready", "active", "retained"}:
            raise ReleaseValidationError(release_id, "release状态尚未ready")
        try:
            target = IndexTarget.from_dict(record["target"])
            serving_profile = record.get("serving_profile")
            if (
                serving_profile is not None
                and sealed_profile_for_target(target, serving_profile) is None
            ):
                raise ReleaseValidationError(release_id, "sealed serving profile与review身份不匹配")
            report = admit_index(target, root=self.root, qdrant_client=self.qdrant_client)
            manifest_path = resolve_project_path(self.root, target.manifest_path)
            bm25_path = resolve_project_path(self.root, target.bm25_path)
            raw_root = target_raw_data_root(self.root, target)
            manifest = manifest_module.validate_manifest(
                manifest_path, raw_data_root=raw_root
            )
            sources = CorpusSources.from_manifest(
                manifest_path,
                raw_root=raw_root,
                expected_corpus_id=target.corpus_identity(),
            )
            source_hashes = {}
            for entry in manifest_module.file_records(manifest):
                digest = file_sha256(sources.locate(entry["path"]))
                if digest != entry["_content_hash"]:
                    raise ReleaseValidationError(
                        release_id, "release原文SHA与manifest不一致"
                    )
                source_hashes[entry["path"]] = digest
            manifest_sha = file_sha256(manifest_path)
            bm25_sha = file_sha256(bm25_path)
            qdrant_sha = qdrant_fingerprint(self.qdrant_client, target.dense_collection)
            source_sha = _hash_mapping(source_hashes)
            digest_payload = {
                "release_id": release_id,
                "library_id": record["library_id"],
                "target": target.__dict__,
                "manifest_sha256": manifest_sha,
                "source_sha256": source_sha,
                "bm25_sha256": bm25_sha,
                "qdrant_sha256": qdrant_sha,
            }
            # 兼容首批没有该字段的已sealed记录；新记录的profile包含在digest内。
            if "serving_profile" in record:
                digest_payload["serving_profile"] = serving_profile
            release_digest = hashlib.sha256(canonical_json(digest_payload)).hexdigest()
            if (
                manifest_sha != record.get("validation", {}).get("manifest_sha256")
                or source_sha != record.get("validation", {}).get("source_sha256")
                or bm25_sha != record.get("validation", {}).get("bm25_sha256")
                or qdrant_sha != record.get("validation", {}).get("qdrant_sha256")
                or release_digest != record.get("release_digest")
            ):
                raise ReleaseValidationError(release_id, "release封存指纹发生变化，拒绝切换")
            return {
                "valid": True,
                "release_id": release_id,
                "target_id": target.target_id,
                "source_files": len(source_hashes),
                "source_sha256": source_sha,
                "manifest_sha256": manifest_sha,
                "bm25_sha256": bm25_sha,
                "qdrant_sha256": qdrant_sha,
                "chunk_count": target.chunk_count,
                "admission": report,
            }
        except ReleaseValidationError:
            raise
        except Exception as error:
            raise ReleaseValidationError(
                release_id, f"release完整性校验失败：{type(error).__name__}"
            ) from error

    def activate(
        self,
        library_id: str,
        release_id: str,
        *,
        expected_revision: int,
        reason: str,
        actor: str | None = None,
        expected_embedding_model: str | None | object = _EXPECTED_MODEL_UNSET,
    ) -> dict:
        snapshot = self.catalog.snapshot(library_id)
        if snapshot["revision"] != expected_revision:
            raise ReleaseConflictError(expected_revision, snapshot["revision"])
        record = self.catalog.get_release(release_id)
        if record["library_id"] != library_id:
            raise ValueError("release不属于指定library_id")
        self._require_embedding_model(record, expected_embedding_model)
        self.validate(release_id)
        return self.catalog.activate(
            library_id,
            release_id,
            expected_revision=expected_revision,
            reason=reason,
            actor=actor,
            expected_release_digest=record["release_digest"],
            expected_active_release_id=snapshot["active_release_id"],
        )

    def rollback(
        self,
        library_id: str,
        *,
        expected_revision: int,
        reason: str,
        actor: str | None = None,
        expected_embedding_model: str | None | object = _EXPECTED_MODEL_UNSET,
    ) -> dict:
        snapshot = self.catalog.snapshot(library_id)
        if snapshot["revision"] != expected_revision:
            raise ReleaseConflictError(expected_revision, snapshot["revision"])
        release_id = self.catalog.previous_release_id(library_id)
        record = self.catalog.get_release(release_id)
        self._require_embedding_model(record, expected_embedding_model)
        self.validate(release_id)
        return self.catalog.rollback(
            library_id,
            expected_revision=expected_revision,
            reason=reason,
            actor=actor,
            expected_release_digest=record["release_digest"],
            expected_active_release_id=snapshot["active_release_id"],
        )

    @staticmethod
    def _require_embedding_model(
        record: dict, expected_embedding_model: str | None | object
    ) -> None:
        if expected_embedding_model is _EXPECTED_MODEL_UNSET:
            # 纯domain/离线调用没有绑定serving进程；在线入口必须显式传入。
            return
        if not isinstance(expected_embedding_model, str) or not expected_embedding_model:
            raise ReleaseValidationError(
                str(record.get("release_id", "unknown")),
                "当前服务Embedding模型身份无效，拒绝切换release",
            )
        target = IndexTarget.from_dict(record["target"])
        if target.embedding_model != expected_embedding_model:
            raise ReleaseValidationError(
                str(record.get("release_id", "unknown")),
                "release声明的Embedding模型与当前服务不一致",
            )


def _normalize_library(value: str) -> str:
    return re.sub(r"[\s_]+", "-", value.strip().lower())


def _hash_mapping(values: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path, item_hash in sorted(values.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _copy_file_exclusive(source: Path, destination: Path) -> None:
    """独占创建并持久化一份文件，不覆盖已存在资源。"""
    with Path(source).open("rb") as incoming, Path(destination).open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())
