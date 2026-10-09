"""统一文件管理的稳定数据结构。"""

from dataclasses import dataclass
from pathlib import Path

from wenjin.retrieval.errors import StaleSnapshotError as StaleSnapshotError  # noqa: F401


@dataclass(frozen=True)
class FileRecord:
    file_id: str
    display_name: str
    status: str
    active_version_id: str | None
    version: str | None
    chunk_count: int | None
    updated_at: str
    duplicate_of: str | None = None
    managed: bool = True
    legacy_id: str | None = None
    library: str | None = None
    chunk_count_source: str | None = None
    latest_job_status: str | None = None
    latest_job_reason: str | None = None
    latest_job_id: str | None = None
    latest_job_phase: str | None = None
    latest_job_progress: int | None = None
    library_id: str | None = None
    library_name: str | None = None
    original_filename: str | None = None


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    file_id: str
    kind: str
    status: str
    phase: str
    progress: int
    retryable: bool
    reason: str | None
    retry_count: int
    created_at: str
    updated_at: str
    duplicate_of: str | None = None
    resources: dict | None = None
    version_id: str | None = None
    version: str | None = None
    file_display_name: str | None = None
    library_id: str | None = None
    library_display_name: str | None = None
    original_filename: str | None = None


@dataclass(frozen=True)
class FileSnapshot:
    file_id: str
    version_id: str
    version: str
    display_name: str
    library_id: str
    library_name: str
    original_filename: str | None
    manifest_path: Path
    raw_root: Path
    bm25_path: Path
    dense_collection: str
    chunk_count: int
    strategy: str
    source_path: Path
    source_raw_sha256: str
