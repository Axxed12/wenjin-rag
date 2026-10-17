"""单文件版本的解析、建索引与验证流程。

组件只持有 service 注入的同一组 adapter、模型锁和连接回调，不创建独立锁或连接。
active 指针切换通过 service 回调完成，因此最终写事务仍由协调器开启。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable

from wenjin.file_management.models import ImportCancelled


class VersionLifecycle:
    """单文件版本的解析、建索引、验证与激活准备。最终切换事务由协调器开启。"""

    def __init__(
        self,
        root: Path,
        base: Path,
        parser,
        index_adapter,
        model_lock,
        connect: Callable,
        update_job: Callable,
        hook: Callable,
        sha256: Callable,
        activate: Callable,
        repository,
        managed_raw_data_root: Path,
        cancel_requested: Callable[[str], bool],
    ):
        self.root = root
        self.managed_raw_data_root = Path(managed_raw_data_root)
        self._cancel_requested = cancel_requested
        self.base = base
        self.parser = parser
        self.index_adapter = index_adapter
        self._model_lock = model_lock
        self._connect = connect
        self._update_job = update_job
        self._hook = hook
        self._sha256 = sha256
        self.repository = repository
        self._activate = activate

    def _run_import(self, job_id: str) -> None:

        from wenjin.ingestion import intake

        with self._connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()

            version_row = db.execute(
                "SELECT * FROM versions WHERE version_id=?", (job["version_id"],)
            ).fetchone()

        payload = json.loads(job["payload"])

        source = Path(payload["source"])

        file_id, version_id, version = job["file_id"], job["version_id"], version_row["version"]

        library = f"file-management-{file_id}"

        corpus_id = version_id

        version_dir = self.base / "versions" / file_id / version_id

        manifest_path = version_dir / "source-manifest.json"

        raw_root = (self.managed_raw_data_root / library / version).resolve()
        if not raw_root.is_relative_to(Path(self.root).resolve()):
            raise ValueError("managed原文目标目录越界")

        indexes_dir = self.base / "indexes" / file_id

        bm25_path = indexes_dir / f"{version_id}.sqlite"

        collection = f"fm_{file_id[:12]}_{version_id[:12]}"

        # 确定资源在创建之前写入版本和job ledger，失败也能安全重试或完整删除。

        self._update_version_resources(
            version_id,
            manifest_path,
            raw_root,
            source.name,
            bm25_path,
            collection,
            0,
        )

        ledger = dict(job and json.loads(job["resources"]))

        ledger.update(
            {
                "manifest": manifest_path.relative_to(self.root).as_posix(),
                "version_dir": version_dir.relative_to(self.root).as_posix(),
                "raw_root": raw_root.relative_to(self.root).as_posix(),
                "bm25": bm25_path.relative_to(self.root).as_posix(),
                "collection": collection,
            }
        )

        self._update_job(
            job_id, status="running", phase="staging", progress=5, reason=None, resources=ledger
        )

        self._hook("staging", {"job_id": job_id, "file_id": file_id})
        self._raise_if_cancel_requested(job_id)

        if not source.is_file() or self._sha256(source) != version_row["raw_sha256"]:
            raise ValueError("暂存原文不存在或SHA256与任务登记不一致")

        pdf_plans, excluded = self._pdf_plans(source)

        self._update_job(job_id, phase="parsing", progress=20)

        self._hook("parsing", {"job_id": job_id, "file_id": file_id})
        self._raise_if_cancel_requested(job_id)

        plan = intake.plan_corpus(
            source=source,
            library=library,
            version=version,
            corpus_id=corpus_id,
            pdf_plans=pdf_plans,
            excluded=excluded,
        )
        self._raise_if_cancel_requested(job_id)

        manifest = intake.build_manifest(plan, scope="文件管理器登记的独立单文件版本。")

        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        self._atomic_json(manifest_path, manifest)

        intake.materialize(plan, raw_root)
        self._raise_if_cancel_requested(job_id)

        counts = intake.retrieve_chunk_counts(manifest_path, self.managed_raw_data_root)
        self._raise_if_cancel_requested(job_id)

        empty = intake.empty_retrieval_entries(manifest, counts)

        if empty:
            raise ValueError(f"可检索文件没有生成chunk：{empty}")

        chunk_count = sum(counts.values())

        if not chunk_count:
            raise ValueError("文件没有生成任何可检索片段")

        self._update_job(job_id, phase="building", progress=40)

        self._hook("building", {"job_id": job_id, "file_id": file_id})
        self._raise_if_cancel_requested(job_id)

        target_id = f"fm-{file_id}-{version_id}"

        build_kwargs = {
            "manifest_path": manifest_path,
            "target_id": target_id,
            "strategy": "blocks-split",
            "collection": collection,
            "bm25_path": bm25_path,
        }
        if getattr(self.index_adapter, "supports_embedding_batch_lock", False):
            target = self.index_adapter.build(**build_kwargs)
        else:
            # 未声明只锁embedding批次能力的自定义adapter保留原建库整体互斥合同。
            with self._model_lock:
                target = self.index_adapter.build(**build_kwargs)
        self._raise_if_cancel_requested(job_id)

        if target.chunk_count != chunk_count:
            raise RuntimeError("建库片段数与入库预解析计数不一致")

        self._update_job(job_id, phase="verifying", progress=75)

        self._hook("verifying", {"job_id": job_id, "collection": collection})
        self._raise_if_cancel_requested(job_id)

        self.index_adapter.verify(target)

        self._update_version_resources(
            version_id,
            manifest_path,
            raw_root,
            source.name,
            bm25_path,
            collection,
            target.chunk_count,
        )

        with self._connect() as db:
            version_row = db.execute(
                "SELECT * FROM versions WHERE version_id=?", (version_id,)
            ).fetchone()

            raw_entries = manifest["files"]

            source_path = raw_root / raw_entries[0]["path"]

        self._update_job(job_id, phase="activating", progress=90)

        self._hook("activating", {"job_id": job_id, "version_id": version_id})
        self._raise_if_cancel_requested(job_id)

        self._activate(
            job_id=job_id,
            file_id=file_id,
            version_id=version_id,
            manifest_path=manifest_path,
            version_dir=version_dir,
            raw_root=raw_root,
            bm25_path=bm25_path,
            collection=collection,
            chunk_count=target.chunk_count,
            source_path=source_path,
            source_sha256=version_row["raw_sha256"],
        )

        staging_dir = self.base / "staging" / job_id

        shutil.rmtree(staging_dir, ignore_errors=True)

    def _raise_if_cancel_requested(self, job_id: str) -> None:
        if self._cancel_requested(job_id):
            raise ImportCancelled("导入任务已请求取消")

    def _pdf_plans(self, source: Path):

        from wenjin.ingestion import intake

        paths = intake.collect_files(source)

        inspections = intake.scan_pdfs(paths)

        if self.parser:
            return self.parser(inspections)

        if not inspections:
            return {}, {}

        from wenjin.ingestion.ocr import (
            check_mineru,
            choose_pdf_parser,
            decide_ocr,
            skipped_scan_pages,
        )

        needs_ocr = any(item.needs_ocr for item in inspections)

        status = check_mineru() if needs_ocr else None

        if needs_ocr and status and status.available:
            decision = decide_ocr(inspections, status=status, mode="always")

        elif needs_ocr:
            decision = decide_ocr(inspections, status=status, mode="never")

        else:
            decision = decide_ocr(inspections, status=None)

        plans = {}

        for item in inspections:
            parser = choose_pdf_parser(item, enabled=decision.enabled)

            plans[item.path] = intake.PdfPlan(
                parser=parser,
                report=item.to_report(),
                skipped_scan_pages=tuple(skipped_scan_pages(item, parser)),
            )

        return plans, decision.excluded

    @staticmethod
    def _atomic_json(path: Path, data: dict) -> None:

        path.parent.mkdir(parents=True, exist_ok=True)

        temporary = path.with_suffix(path.suffix + ".tmp")

        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        os.replace(temporary, path)

    def _update_version_resources(
        self, version_id, manifest_path, raw_root, source_name, bm25_path, collection, chunk_count
    ) -> None:

        with self._connect() as db:
            self.repository.update_version_resources(
                db,
                version_id,
                manifest_path,
                raw_root,
                source_name,
                bm25_path,
                collection,
                chunk_count,
            )
