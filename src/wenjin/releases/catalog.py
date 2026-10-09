"""基于SQLite的多资料库release catalog及CAS active指针。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_EXPECTED_ACTIVE_UNSET = object()


class ReleaseConflictError(RuntimeError):
    """active revision已变化，调用方需要重新读取后决定是否重试。"""

    code = "revision_conflict"

    def __init__(
        self,
        expected_revision: int,
        current_revision: int,
        *,
        expected_active_release_id: str | None = None,
        current_active_release_id: str | None = None,
    ) -> None:
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        self.expected_active_release_id = expected_active_release_id
        self.current_active_release_id = current_active_release_id
        active_detail = ""
        if expected_active_release_id != current_active_release_id:
            active_detail = (
                f"；active由{expected_active_release_id!r}变为"
                f"{current_active_release_id!r}"
            )
        super().__init__(
            f"release catalog revision冲突：期望{expected_revision}，当前{current_revision}"
            f"{active_detail}"
        )


class ReleaseNotFoundError(KeyError):
    """release id不在本地catalog中。"""

    code = "release_not_found"


class ReleaseCatalog:
    """活动指针和release元数据的SQLite存储。

    `revision`仅在active指针切换时增长；prepare/validate状态更新不让并行候选构建
    意外使另一个管理员的切换请求过期。BEGIN IMMEDIATE为本机多进程写者串行化。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    revision INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO catalog_meta(singleton, revision) VALUES(1, 0);
                CREATE TABLE IF NOT EXISTS libraries (
                    library_id TEXT PRIMARY KEY,
                    active_release_id TEXT
                );
                CREATE TABLE IF NOT EXISTS releases (
                    release_id TEXT PRIMARY KEY,
                    library_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(library_id) REFERENCES libraries(library_id)
                );
                CREATE INDEX IF NOT EXISTS releases_by_library
                    ON releases(library_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS library_history (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    library_id TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    activated_revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS release_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    action TEXT NOT NULL,
                    library_id TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    previous_release_id TEXT,
                    revision INTEGER NOT NULL,
                    actor TEXT,
                    reason TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _connect(self):
        database = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA foreign_keys = ON")
            database.execute("PRAGMA busy_timeout = 30000")
            yield database
        finally:
            database.close()

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        payload["status"] = row["status"]
        return payload

    def revision(self) -> int:
        with self._connect() as database:
            return int(database.execute(
                "SELECT revision FROM catalog_meta WHERE singleton=1"
            ).fetchone()[0])

    def register(self, record: dict[str, Any]) -> dict[str, Any]:
        """加入一个preparing release；不改变当前active revision。"""
        required = {"release_id", "library_id", "status", "created_at"}
        if not required.issubset(record):
            raise ValueError("release记录缺少必要字段")
        payload = dict(record)
        library_id = str(payload["library_id"])
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                database.execute(
                    "INSERT OR IGNORE INTO libraries(library_id, active_release_id) VALUES(?, NULL)",
                    (library_id,),
                )
                database.execute(
                    "INSERT INTO releases(release_id, library_id, status, payload_json, created_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        payload["release_id"], library_id, payload["status"],
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        payload["created_at"],
                    ),
                )
                database.commit()
            except Exception:
                database.rollback()
                raise
        return payload

    def mark_ready(self, release_id: str, *, seal: dict[str, Any]) -> dict[str, Any]:
        """封存一次preparing记录；ready之后target与指纹不再可编辑。"""
        required_seal = {"target", "release_digest", "source_hashes", "validation"}
        if not required_seal.issubset(seal):
            raise ValueError("release seal缺少target或完整性指纹")
        if not isinstance(seal["release_digest"], str) or len(seal["release_digest"]) != 64:
            raise ValueError("release_digest必须是SHA256")
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                row = database.execute(
                    "SELECT * FROM releases WHERE release_id=?", (release_id,)
                ).fetchone()
                if row is None:
                    raise ReleaseNotFoundError(release_id)
                if row["status"] != "preparing":
                    raise ValueError("只能封存preparing release")
                payload = json.loads(row["payload_json"])
                payload.update(seal)
                payload["status"] = "ready"
                database.execute(
                    "UPDATE releases SET status=?, payload_json=? WHERE release_id=?",
                    ("ready", json.dumps(payload, ensure_ascii=False, sort_keys=True), release_id),
                )
                database.commit()
            except Exception:
                database.rollback()
                raise
        return payload

    def mark_failed(self, release_id: str, *, failure: dict[str, Any]) -> dict[str, Any]:
        """记录prepare失败原因，不清理或改动本次登记的owned资源。"""
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                row = database.execute(
                    "SELECT * FROM releases WHERE release_id=?", (release_id,)
                ).fetchone()
                if row is None:
                    raise ReleaseNotFoundError(release_id)
                if row["status"] not in {"preparing", "failed"}:
                    raise ValueError("ready release的sealed metadata不能改写")
                payload = json.loads(row["payload_json"])
                payload["failure"] = failure
                payload["status"] = "failed"
                database.execute(
                    "UPDATE releases SET status='failed', payload_json=? WHERE release_id=?",
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True), release_id),
                )
                database.commit()
            except Exception:
                database.rollback()
                raise
        return payload

    def get_release(self, release_id: str) -> dict[str, Any]:
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM releases WHERE release_id=?", (release_id,)
            ).fetchone()
        record = self._decode(row)
        if record is None:
            raise ReleaseNotFoundError(release_id)
        return record

    def snapshot(self, library_id: str) -> dict[str, Any]:
        all_state = self.snapshot_all()
        return all_state["libraries"].get(library_id, {
            "library_id": library_id,
            "active_release_id": None,
            "active": None,
            "releases": [],
            "history": [],
        }) | {"revision": all_state["revision"]}

    def snapshot_all(self) -> dict[str, Any]:
        """一次SQLite读事务读取所有库的active指针，供多库请求固定统一revision。"""
        with self._connect() as database:
            database.execute("BEGIN")
            revision = int(database.execute(
                "SELECT revision FROM catalog_meta WHERE singleton=1"
            ).fetchone()[0])
            library_rows = database.execute(
                "SELECT library_id, active_release_id FROM libraries ORDER BY library_id"
            ).fetchall()
            release_rows = database.execute(
                "SELECT * FROM releases ORDER BY created_at DESC, release_id"
            ).fetchall()
            history_rows = database.execute(
                "SELECT library_id, release_id FROM library_history ORDER BY sequence"
            ).fetchall()
            activated_rows = database.execute(
                "SELECT DISTINCT library_id FROM release_events "
                "WHERE action IN ('activate', 'rollback')"
            ).fetchall()
            database.commit()
        releases_by_library: dict[str, list[dict[str, Any]]] = {}
        for row in release_rows:
            record = self._decode(row)
            releases_by_library.setdefault(record["library_id"], []).append(record)
        history_by_library: dict[str, list[str]] = {}
        for row in history_rows:
            history_by_library.setdefault(row["library_id"], []).append(row["release_id"])
        activated_libraries = {row["library_id"] for row in activated_rows}
        libraries = {}
        for row in library_rows:
            library_id = row["library_id"]
            releases = releases_by_library.get(library_id, [])
            active_id = row["active_release_id"]
            active = next(
                (item for item in releases if item["release_id"] == active_id), None
            )
            libraries[library_id] = {
                "library_id": library_id,
                "active_release_id": active_id,
                "active": active,
                "releases": releases,
                "history": history_by_library.get(library_id, []),
                # active=None 可能是首次prepare，也可能是历史active指针丢失；
                # 用持久事件区分，避免已迁移库静默回退到旧索引。
                "ever_activated": library_id in activated_libraries,
            }
        return {"revision": revision, "libraries": libraries}

    def previous_release_id(self, library_id: str) -> str:
        with self._connect() as database:
            row = database.execute(
                "SELECT release_id FROM library_history WHERE library_id=? "
                "ORDER BY sequence DESC LIMIT 1",
                (library_id,),
            ).fetchone()
        if row is None:
            raise ValueError("该资料库没有可回退的release")
        return str(row[0])

    def events(self, library_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("event limit必须在1到1000之间")
        with self._connect() as database:
            rows = database.execute(
                "SELECT timestamp_utc, action, library_id, release_id, previous_release_id, "
                "revision, actor, reason FROM release_events WHERE library_id=? "
                "ORDER BY event_id DESC LIMIT ?",
                (library_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def activate(
        self,
        library_id: str,
        release_id: str,
        *,
        expected_revision: int,
        reason: str,
        actor: str | None = None,
        expected_release_digest: str | None = None,
        expected_active_release_id: str | None | object = _EXPECTED_ACTIVE_UNSET,
    ) -> dict[str, Any]:
        return self._switch(
            library_id,
            release_id,
            expected_revision=expected_revision,
            reason=reason,
            actor=actor,
            action="activate",
            pop_history=False,
            expected_release_digest=expected_release_digest,
            expected_active_release_id=expected_active_release_id,
        )

    def rollback(
        self,
        library_id: str,
        *,
        expected_revision: int,
        reason: str,
        actor: str | None = None,
        expected_release_digest: str | None = None,
        expected_active_release_id: str | None | object = _EXPECTED_ACTIVE_UNSET,
    ) -> dict[str, Any]:
        target_release_id = self.previous_release_id(library_id)
        return self._switch(
            library_id,
            target_release_id,
            expected_revision=expected_revision,
            reason=reason,
            actor=actor,
            action="rollback",
            pop_history=True,
            expected_release_digest=expected_release_digest,
            expected_active_release_id=expected_active_release_id,
        )

    def _switch(
        self,
        library_id: str,
        release_id: str,
        *,
        expected_revision: int,
        reason: str,
        actor: str | None,
        action: str,
        pop_history: bool,
        expected_release_digest: str | None,
        expected_active_release_id: str | None | object,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("切换/回退必须记录reason")
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                revision = int(database.execute(
                    "SELECT revision FROM catalog_meta WHERE singleton=1"
                ).fetchone()[0])
                if revision != expected_revision:
                    raise ReleaseConflictError(expected_revision, revision)
                row = database.execute(
                    "SELECT status, payload_json FROM releases WHERE release_id=? AND library_id=?",
                    (release_id, library_id),
                ).fetchone()
                if row is None:
                    raise ReleaseNotFoundError(release_id)
                if row[0] not in {"ready", "active", "retained"}:
                    raise ValueError(f"release状态不可激活：{row[0]}")
                if expected_release_digest is not None:
                    sealed = json.loads(row["payload_json"]).get("release_digest")
                    if sealed != expected_release_digest:
                        raise ValueError("release seal在校验后发生变化，拒绝切换")
                library = database.execute(
                    "SELECT active_release_id FROM libraries WHERE library_id=?", (library_id,)
                ).fetchone()
                previous_release_id = library[0] if library else None
                if (
                    expected_active_release_id is not _EXPECTED_ACTIVE_UNSET
                    and previous_release_id != expected_active_release_id
                ):
                    raise ReleaseConflictError(
                        expected_revision,
                        revision,
                        expected_active_release_id=expected_active_release_id,
                        current_active_release_id=previous_release_id,
                    )
                if previous_release_id == release_id:
                    raise ValueError("release已经是active")
                next_revision = revision + 1
                if pop_history:
                    database.execute(
                        "DELETE FROM library_history WHERE sequence=(SELECT MAX(sequence) "
                        "FROM library_history WHERE library_id=?)",
                        (library_id,),
                    )
                if previous_release_id is not None:
                    database.execute(
                        "INSERT INTO library_history(library_id, release_id, activated_revision) "
                        "VALUES(?, ?, ?)",
                        (library_id, previous_release_id, revision),
                    )
                    database.execute(
                        "UPDATE releases SET status='retained' WHERE release_id=?",
                        (previous_release_id,),
                    )
                database.execute(
                    "UPDATE libraries SET active_release_id=? WHERE library_id=?",
                    (release_id, library_id),
                )
                database.execute(
                    "UPDATE releases SET status='active' WHERE release_id=?", (release_id,)
                )
                database.execute(
                    "UPDATE catalog_meta SET revision=? WHERE singleton=1", (next_revision,)
                )
                database.execute(
                    "INSERT INTO release_events(timestamp_utc, action, library_id, release_id, "
                    "previous_release_id, revision, actor, reason) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        datetime.now(timezone.utc).isoformat(), action, library_id, release_id,
                        previous_release_id, next_revision, actor, reason,
                    ),
                )
                release_rows = database.execute(
                    "SELECT * FROM releases WHERE library_id=? "
                    "ORDER BY created_at DESC, release_id",
                    (library_id,),
                ).fetchall()
                releases = [self._decode(item) for item in release_rows]
                history_rows = database.execute(
                    "SELECT release_id FROM library_history WHERE library_id=? ORDER BY sequence",
                    (library_id,),
                ).fetchall()
                committed_snapshot = {
                    "revision": next_revision,
                    "library_id": library_id,
                    "active_release_id": release_id,
                    "active": next(
                        item for item in releases if item["release_id"] == release_id
                    ),
                    "releases": releases,
                    "history": [item[0] for item in history_rows],
                }
                database.commit()
            except Exception:
                database.rollback()
                raise
        return committed_snapshot
