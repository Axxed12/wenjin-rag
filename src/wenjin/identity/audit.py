"""身份域审计行的安全投影。禁止记录秘密和任意请求正文。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

_METADATA_KEYS = frozenset({
    "role", "revision", "resource_kind", "active", "credential_prefix",
    "reason_code", "username_sha256", "principal_kind", "previous_revision",
    "previous_role", "previous_active", "previous_allowed", "allowed",
    "principal_id", "user_id", "classification", "previous_classification",
})


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def append_event(
    connection, *, actor_user_id: str | None, action: str, target_kind: str,
    target_id: str | None, outcome: str = "success", metadata: dict | None = None,
    request_id: str | None = None,
) -> str:
    safe_metadata = {
        key: value for key, value in (metadata or {}).items()
        if key in _METADATA_KEYS and isinstance(value, (str, int, bool, type(None)))
    }
    event_id = uuid4().hex
    connection.execute(
        "INSERT INTO audit_events(event_id,occurred_at,actor_user_id,action,target_kind,"
        "target_id,outcome,request_id,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (event_id, timestamp(), actor_user_id, action[:80], target_kind[:40],
         target_id[:160] if target_id else None, outcome[:40],
         request_id[:80] if request_id else None,
         json.dumps(safe_metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
    )
    return event_id
