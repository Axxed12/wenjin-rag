"""SQLite权威身份库：用户、Web会话、个人MCP token、grant和事务审计。"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from wenjin.identity.audit import append_event
from wenjin.identity.passwords import dummy_password_record, hash_password, verify_password
from wenjin.identity.policy import (
    RESOURCE_KINDS,
    ROLES,
    AuthenticationFailed,
    Conflict,
    CurrentPasswordIncorrect,
    Forbidden,
    GrantRecord,
    IssuedCredential,
    LoginSession,
    NotFound,
    Principal,
    UserRecord,
)
from wenjin.identity.tokens import issue_token, require_token_kind

SCHEMA_VERSION = 2
DEFAULT_IDLE_SECONDS = 30 * 60
DEFAULT_ABSOLUTE_SECONDS = 12 * 60 * 60
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class IdentityStore:
    """身份域存储。构造函数必须收到明确数据库路径，不读取环境变量或默认创建账户。"""

    def __init__(
        self,
        db_path: Path,
        *,
        session_idle_seconds: int = DEFAULT_IDLE_SECONDS,
        session_absolute_seconds: int = DEFAULT_ABSOLUTE_SECONDS,
        busy_timeout_ms: int = 10_000,
    ) -> None:
        if not isinstance(db_path, Path):
            raise TypeError("db_path必须是明确的Path")
        if not db_path.is_absolute():
            raise ValueError("身份数据库路径必须是绝对Path")
        if session_idle_seconds < 60 or session_absolute_seconds < session_idle_seconds:
            raise ValueError("Web会话时限无效")
        if busy_timeout_ms < 100 or busy_timeout_ms > 120_000:
            raise ValueError("SQLite busy timeout必须在100到120000毫秒之间")
        self.db_path = db_path
        self.session_idle_seconds = session_idle_seconds
        self.session_absolute_seconds = session_absolute_seconds
        self.busy_timeout_ms = busy_timeout_ms

    def initialize(self) -> None:
        """创建本地身份库结构；不创建首位管理员、不迁移任何ACL。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 首次未知用户登录也只做一次密码KDF；后续与已知用户名的校验成本接近。
        dummy_password_record()
        with self._connect(allow_create=True) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise RuntimeError("身份数据库版本高于当前代码，拒绝降级打开")
                if version == 0:
                    self._create_schema(connection)
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                elif version == 1:
                    self._migrate_v1_to_v2(connection)
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def _connect(self, *, allow_create: bool = False) -> Iterator[sqlite3.Connection]:
        if not allow_create and not self.db_path.is_file():
            raise RuntimeError("身份数据库尚未初始化")
        connection = sqlite3.connect(
            self.db_path, timeout=self.busy_timeout_ms / 1000,
            isolation_level=None, check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        try:
            if not allow_create:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version != SCHEMA_VERSION:
                    raise RuntimeError("身份数据库尚未初始化或版本不兼容")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        schema = """
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                username_norm TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL CHECK(role IN ('admin','reader')),
                active INTEGER NOT NULL CHECK(active IN (0,1)),
                password_scheme TEXT NOT NULL,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                must_change_password INTEGER NOT NULL CHECK(must_change_password IN (0,1)),
                revision INTEGER NOT NULL CHECK(revision >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX users_active_role_idx ON users(active, role);

            CREATE TABLE web_sessions (
                session_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                token_prefix TEXT NOT NULL,
                csrf_hash TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(user_id),
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE INDEX web_sessions_user_idx ON web_sessions(user_id, revoked_at);

            CREATE TABLE mcp_credentials (
                credential_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                token_prefix TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(user_id),
                label TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                revoked_at TEXT
            );
            CREATE INDEX mcp_credentials_user_idx ON mcp_credentials(user_id, revoked_at);

            CREATE TABLE resources (
                resource_kind TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                active INTEGER NOT NULL CHECK(active IN (0,1)),
                revision INTEGER NOT NULL CHECK(revision >= 1),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(resource_kind, resource_id)
            );
            CREATE TABLE resource_grants (
                resource_kind TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                principal_kind TEXT NOT NULL CHECK(principal_kind IN ('user','authenticated')),
                principal_id TEXT NOT NULL,
                allowed INTEGER NOT NULL CHECK(allowed IN (0,1)),
                revision INTEGER NOT NULL CHECK(revision >= 1),
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL REFERENCES users(user_id),
                PRIMARY KEY(resource_kind, resource_id, principal_kind, principal_id),
                FOREIGN KEY(resource_kind, resource_id)
                    REFERENCES resources(resource_kind, resource_id)
            );
            CREATE INDEX resource_grants_user_idx ON resource_grants(principal_kind, principal_id, allowed);

            CREATE TABLE library_classifications (
                library_id TEXT PRIMARY KEY,
                classification TEXT NOT NULL CHECK(classification IN ('public','private')),
                revision INTEGER NOT NULL CHECK(revision >= 1),
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL REFERENCES users(user_id)
            );

            CREATE TABLE audit_events (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                actor_user_id TEXT REFERENCES users(user_id),
                action TEXT NOT NULL,
                target_kind TEXT NOT NULL,
                target_id TEXT,
                outcome TEXT NOT NULL,
                request_id TEXT,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX audit_events_time_idx ON audit_events(occurred_at DESC, event_id);
            CREATE INDEX audit_events_actor_idx ON audit_events(actor_user_id, occurred_at DESC);
            """
        # executescript 会隐式提交当前事务；逐条执行以保留schema创建和user_version的原子性。
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(statement)

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE library_classifications ("
            "library_id TEXT PRIMARY KEY,"
            "classification TEXT NOT NULL CHECK(classification IN ('public','private')),"
            "revision INTEGER NOT NULL CHECK(revision >= 1),"
            "updated_at TEXT NOT NULL,"
            "updated_by TEXT NOT NULL REFERENCES users(user_id))"
        )

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _stamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _username(username: str) -> tuple[str, str]:
        if not isinstance(username, str):
            raise ValueError("用户名必须是字符串")
        display = username.strip()
        if not _USERNAME_RE.fullmatch(display):
            raise ValueError("用户名须为2到64位字母、数字、点、下划线或连字符")
        return display, unicodedata.normalize("NFKC", display).casefold()

    @staticmethod
    def _resource(resource_kind: str, resource_id: str) -> tuple[str, str]:
        if resource_kind not in RESOURCE_KINDS:
            raise ValueError("资料库资源类型无效")
        if not isinstance(resource_id, str) or not _RESOURCE_ID_RE.fullmatch(resource_id):
            raise ValueError("稳定资源ID格式无效")
        return resource_kind, resource_id

    @staticmethod
    def _user_record(row: sqlite3.Row) -> UserRecord:
        return UserRecord(
            user_id=row["user_id"], username=row["username"], role=row["role"],
            active=bool(row["active"]),
            must_change_password=bool(row["must_change_password"]),
            revision=int(row["revision"]), created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _principal(row: sqlite3.Row) -> Principal:
        return Principal(
            user_id=row["user_id"], username=row["username"], role=row["role"],
            revision=int(row["revision"]),
            must_change_password=bool(row["must_change_password"])
            if "must_change_password" in row.keys() else False,
        )

    @staticmethod
    def _require_admin(connection: sqlite3.Connection, actor: Principal) -> sqlite3.Row:
        if not isinstance(actor, Principal):
            raise Forbidden("需要已解析的管理员身份")
        row = connection.execute(
            "SELECT * FROM users WHERE user_id=?", (actor.user_id,)
        ).fetchone()
        if (row is None or not row["active"] or row["role"] != "admin"
                or row["must_change_password"]):
            raise Forbidden("当前账号不能执行管理员操作")
        return row

    @staticmethod
    def _require_active_user(connection: sqlite3.Connection, user_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM users WHERE user_id=? AND active=1", (user_id,)
        ).fetchone()
        if row is None:
            raise AuthenticationFailed("账号不可用")
        return row

    @staticmethod
    def _revoke_user_secrets(connection: sqlite3.Connection, user_id: str, now: str) -> None:
        connection.execute(
            "UPDATE web_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now, user_id),
        )
        connection.execute(
            "UPDATE mcp_credentials SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now, user_id),
        )

    @staticmethod
    def _revision(connection: sqlite3.Connection, table: str, where: str, params: tuple) -> int:
        if table not in {"users", "resources", "resource_grants"}:
            raise ValueError("不支持读取该表revision")
        row = connection.execute(f"SELECT revision FROM {table} WHERE {where}", params).fetchone()
        if row is None:
            raise NotFound("目标不存在")
        return int(row["revision"])

    @staticmethod
    def _event(connection, actor_user_id, action, target_kind, target_id, *, metadata=None, outcome="success"):
        return append_event(
            connection, actor_user_id=actor_user_id, action=action,
            target_kind=target_kind, target_id=target_id, metadata=metadata,
            outcome=outcome,
        )

    @staticmethod
    def _new_user(
        connection: sqlite3.Connection, *, username: str, username_norm: str,
        role: str, password: str, must_change_password: bool, now: str,
    ) -> UserRecord:
        if role not in ROLES:
            raise ValueError("角色只能是admin或reader")
        scheme, salt, password_hash = hash_password(password)
        user_id = uuid4().hex
        try:
            connection.execute(
                "INSERT INTO users(user_id,username,username_norm,role,active,password_scheme,"
                "password_salt,password_hash,must_change_password,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,1,?,?,?, ?,1,?,?)",
                (user_id, username, username_norm, role, scheme, salt, password_hash,
                 int(must_change_password), now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户名已存在") from exc
        row = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return IdentityStore._user_record(row)

    def bootstrap_first_admin(self, username: str, password: str) -> UserRecord:
        """仅在用户表为空时一次性创建首位管理员；事务内再次检查空态。"""
        display, normalized = self._username(username)
        now = self._stamp(self._now())
        with self._transaction(write=True) as connection:
            if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
                raise Conflict("首位管理员bootstrap已关闭")
            user = self._new_user(
                connection, username=display, username_norm=normalized, role="admin",
                password=password, must_change_password=False, now=now,
            )
            self._event(connection, None, "bootstrap_first_admin", "user", user.user_id,
                        metadata={"role": "admin"})
            return user

    def create_user(
        self, actor: Principal, username: str, password: str, *, role: str = "reader",
    ) -> UserRecord:
        """管理员创建用户；新账户首次登录必须修改临时密码。"""
        display, normalized = self._username(username)
        now = self._stamp(self._now())
        with self._transaction(write=True) as connection:
            admin = self._require_admin(connection, actor)
            user = self._new_user(
                connection, username=display, username_norm=normalized, role=role,
                password=password, must_change_password=True, now=now,
            )
            self._event(connection, admin["user_id"], "user_created", "user", user.user_id,
                        metadata={"role": role})
            return user

    def list_users(self, actor: Principal) -> list[UserRecord]:
        with self._transaction() as connection:
            self._require_admin(connection, actor)
            rows = connection.execute("SELECT * FROM users ORDER BY username_norm").fetchall()
            return [self._user_record(row) for row in rows]

    def bootstrap_is_open(self) -> bool:
        with self._transaction() as connection:
            return connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None

    def set_user_role(
        self, actor: Principal, user_id: str, role: str, *, expected_revision: int,
    ) -> UserRecord:
        if role not in ROLES:
            raise ValueError("角色只能是admin或reader")
        now = self._stamp(self._now())
        with self._transaction(write=True) as connection:
            admin = self._require_admin(connection, actor)
            target = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if target is None:
                raise NotFound("用户不存在")
            if int(target["revision"]) != expected_revision:
                raise Conflict("用户记录已变化，请刷新后重试")
            if admin["user_id"] == user_id and role != "admin":
                raise Conflict("不能在当前会话中降低自己的管理员角色")
            if target["active"] and target["role"] == "admin" and role != "admin":
                count = connection.execute(
                    "SELECT COUNT(*) AS n FROM users WHERE active=1 AND role='admin'"
                ).fetchone()["n"]
                if count <= 1:
                    raise Conflict("不能移除最后一个启用的管理员")
            connection.execute(
                "UPDATE users SET role=?,revision=revision+1,updated_at=? WHERE user_id=? AND revision=?",
                (role, now, user_id, expected_revision),
            )
            updated = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            self._event(connection, admin["user_id"], "user_role_changed", "user", user_id,
                        metadata={"previous_role": target["role"], "role": role,
                                  "previous_revision": expected_revision,
                                  "revision": int(updated["revision"])})
            return self._user_record(updated)

    def rename_user(
        self, actor: Principal, user_id: str, username: str, *, expected_revision: int,
    ) -> UserRecord:
        display, normalized = self._username(username)
        now = self._stamp(self._now())
        with self._transaction(write=True) as connection:
            admin = self._require_admin(connection, actor)
            target = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if target is None:
                raise NotFound("用户不存在")
            if int(target["revision"]) != expected_revision:
                raise Conflict("用户记录已变化，请刷新后重试")
            try:
                connection.execute(
                    "UPDATE users SET username=?,username_norm=?,revision=revision+1,updated_at=? "
                    "WHERE user_id=? AND revision=?",
                    (display, normalized, now, user_id, expected_revision),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("用户名已存在") from exc
            updated = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            self._event(connection, admin["user_id"], "user_renamed", "user", user_id,
                        metadata={"previous_revision": expected_revision,
                                  "revision": int(updated["revision"])})
            return self._user_record(updated)

    def set_user_active(
        self, actor: Principal, user_id: str, active: bool, *, expected_revision: int,
    ) -> UserRecord:
        if not isinstance(active, bool):
            raise ValueError("active必须是布尔值")
        now = self._stamp(self._now())
        with self._transaction(write=True) as connection:
            admin = self._require_admin(connection, actor)
            target = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if target is None:
                raise NotFound("用户不存在")
            if int(target["revision"]) != expected_revision:
                raise Conflict("用户记录已变化，请刷新后重试")
            if admin["user_id"] == user_id and not active:
                raise Conflict("不能在当前会话中停用自己的管理员账号")
            if target["active"] and not active and target["role"] == "admin":
                count = connection.execute(
                    "SELECT COUNT(*) AS n FROM users WHERE active=1 AND role='admin'"
                ).fetchone()["n"]
                if count <= 1:
                    raise Conflict("不能停用最后一个启用的管理员")
            connection.execute(
                "UPDATE users SET active=?,revision=revision+1,updated_at=? WHERE user_id=? AND revision=?",
                (int(active), now, user_id, expected_revision),
            )
            if not active:
                self._revoke_user_secrets(connection, user_id, now)
            updated = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            self._event(connection, admin["user_id"], "user_activation_changed", "user", user_id,
                        metadata={"previous_active": bool(target["active"]), "active": active,
                                  "previous_revision": expected_revision,
                                  "revision": int(updated["revision"])})
            return self._user_record(updated)

    def reset_password(
        self, actor: Principal, user_id: str, new_password: str, *, expected_revision: int,
    ) -> UserRecord:
        """管理员重置密码并撤销目标用户全部Web会话与MCP凭据。"""
        now = self._stamp(self._now())
        with self._transaction(write=True) as connection:
            admin = self._require_admin(connection, actor)
            target = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if target is None:
                raise NotFound("用户不存在")
            if int(target["revision"]) != expected_revision:
                raise Conflict("用户记录已变化，请刷新后重试")
            scheme, salt, password_hash = hash_password(new_password)
            connection.execute(
                "UPDATE users SET password_scheme=?,password_salt=?,password_hash=?,"
                "must_change_password=1,revision=revision+1,updated_at=? WHERE user_id=? AND revision=?",
                (scheme, salt, password_hash, now, user_id, expected_revision),
            )
            self._revoke_user_secrets(connection, user_id, now)
            updated = connection.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            self._event(connection, admin["user_id"], "user_password_reset", "user", user_id,
                        metadata={"previous_revision": expected_revision,
                                  "revision": int(updated["revision"])})
            return self._user_record(updated)

    def login(self, username: str, password: str) -> LoginSession:
        display, normalized = self._username(username)
        now_dt = self._now()
        now = self._stamp(now_dt)
        expires = self._stamp(now_dt + timedelta(seconds=self.session_absolute_seconds))
        result: LoginSession | None = None
        with self._transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username_norm=?", (normalized,)
            ).fetchone()
            scheme, salt, expected = (
                (row["password_scheme"], row["password_salt"], row["password_hash"])
                if row is not None else dummy_password_record()
            )
            password_matches = verify_password(password, scheme, salt, expected)
            if row is None or not row["active"] or not password_matches:
                username_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                self._event(connection, None, "login_failed", "username", username_hash,
                            outcome="denied", metadata={"username_sha256": username_hash})
            else:
                token, token_hash, prefix = issue_token("session")
                csrf_token = secrets.token_urlsafe(32)
                csrf_hash = hashlib.sha256(csrf_token.encode("ascii")).hexdigest()
                session_id = uuid4().hex
                connection.execute(
                    "INSERT INTO web_sessions(session_id,token_hash,token_prefix,csrf_hash,user_id,"
                    "created_at,last_seen_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                    (session_id, token_hash, prefix, csrf_hash, row["user_id"], now, now, expires),
                )
                self._event(connection, row["user_id"], "login_succeeded", "session", session_id)
                result = LoginSession(
                    token=token, csrf_token=csrf_token, principal=self._principal(row), expires_at=expires,
                    must_change_password=bool(row["must_change_password"]),
                )
        if result is None:
            raise AuthenticationFailed("用户名或密码错误")
        return result

    def resolve_session(self, token: str) -> Principal:
        try:
            digest = require_token_kind(token, "session")
        except ValueError as exc:
            raise AuthenticationFailed("会话不可用") from exc
        now_dt = self._now()
        now = self._stamp(now_dt)
        with self._transaction(write=True) as connection:
            row = connection.execute(
                "SELECT s.*,u.user_id,u.username,u.role,u.revision,u.active,u.must_change_password "
                "FROM web_sessions s JOIN users u ON u.user_id=s.user_id "
                "WHERE s.token_hash=?", (digest,),
            ).fetchone()
            if row is None or row["revoked_at"] is not None or not row["active"]:
                raise AuthenticationFailed("会话不可用")
            if now_dt >= datetime.fromisoformat(row["expires_at"]):
                raise AuthenticationFailed("会话已过期")
            if now_dt - datetime.fromisoformat(row["last_seen_at"]) >= timedelta(seconds=self.session_idle_seconds):
                raise AuthenticationFailed("会话已过期")
            if now_dt - datetime.fromisoformat(row["last_seen_at"]) >= timedelta(seconds=60):
                connection.execute("UPDATE web_sessions SET last_seen_at=? WHERE session_id=?", (now, row["session_id"]))
            return self._principal(row)

    def verify_csrf(self, session_token: str, csrf_token: str) -> bool:
        session_digest = require_token_kind(session_token, "session")
        if not isinstance(csrf_token, str) or not 32 <= len(csrf_token) <= 128:
            return False
        try:
            supplied = hashlib.sha256(csrf_token.encode("ascii")).hexdigest()
        except UnicodeEncodeError:
            return False
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT s.csrf_hash,s.revoked_at,s.expires_at,s.last_seen_at,u.active "
                "FROM web_sessions s JOIN users u ON u.user_id=s.user_id WHERE s.token_hash=?",
                (session_digest,),
            ).fetchone()
            if (row is None or row["revoked_at"] is not None or not row["active"]
                    or now >= datetime.fromisoformat(row["expires_at"])
                    or now - datetime.fromisoformat(row["last_seen_at"])
                    >= timedelta(seconds=self.session_idle_seconds)):
                return False
            return secrets.compare_digest(supplied, row["csrf_hash"])

    def rotate_csrf(self, session_token: str) -> str:
        """给已验证的同源页面发新CSRF值，支持页面重载而不暴露会话token。"""
        digest = require_token_kind(session_token, "session")
        now_dt = self._now()
        csrf_token = secrets.token_urlsafe(32)
        csrf_hash = hashlib.sha256(csrf_token.encode("ascii")).hexdigest()
        with self._transaction(write=True) as connection:
            row = connection.execute(
                "SELECT s.session_id,s.user_id,s.revoked_at,s.expires_at,s.last_seen_at,u.active "
                "FROM web_sessions s JOIN users u ON u.user_id=s.user_id WHERE s.token_hash=?",
                (digest,),
            ).fetchone()
            if (row is None or row["revoked_at"] is not None or not row["active"]
                    or now_dt >= datetime.fromisoformat(row["expires_at"])
                    or now_dt - datetime.fromisoformat(row["last_seen_at"])
                    >= timedelta(seconds=self.session_idle_seconds)):
                raise AuthenticationFailed("会话不可用")
            connection.execute("UPDATE web_sessions SET csrf_hash=? WHERE session_id=?",
                               (csrf_hash, row["session_id"]))
        return csrf_token

    def logout(self, token: str) -> None:
        digest = require_token_kind(token, "session")
        now = self._stamp(self._now())
        with self._transaction(write=True) as connection:
            row = connection.execute(
                "SELECT session_id,user_id FROM web_sessions WHERE token_hash=? AND revoked_at IS NULL",
                (digest,),
            ).fetchone()
            if row is None:
                return
            connection.execute("UPDATE web_sessions SET revoked_at=? WHERE session_id=?", (now, row["session_id"]))
            self._event(connection, row["user_id"], "logout", "session", row["session_id"])

    def change_password(self, session_token: str, old_password: str, new_password: str) -> UserRecord:
        digest = require_token_kind(session_token, "session")
        now_dt = self._now()
        now = self._stamp(now_dt)
        result: UserRecord | None = None
        invalid_old_password = False
        with self._transaction(write=True) as connection:
            session = connection.execute(
                "SELECT s.*,u.* FROM web_sessions s JOIN users u ON u.user_id=s.user_id "
                "WHERE s.token_hash=?", (digest,),
            ).fetchone()
            if (session is None or session["revoked_at"] is not None or not session["active"]
                    or now_dt >= datetime.fromisoformat(session["expires_at"])
                    or now_dt - datetime.fromisoformat(session["last_seen_at"])
                    >= timedelta(seconds=self.session_idle_seconds)):
                raise AuthenticationFailed("会话不可用")
            if not verify_password(
                old_password, session["password_scheme"], session["password_salt"], session["password_hash"]
            ):
                self._event(connection, session["user_id"], "password_change_failed", "user", session["user_id"], outcome="denied")
                invalid_old_password = True
            else:
                scheme, salt, password_hash = hash_password(new_password)
                connection.execute(
                    "UPDATE users SET password_scheme=?,password_salt=?,password_hash=?,"
                    "must_change_password=0,revision=revision+1,updated_at=? WHERE user_id=?",
                    (scheme, salt, password_hash, now, session["user_id"]),
                )
                self._revoke_user_secrets(connection, session["user_id"], now)
                updated = connection.execute("SELECT * FROM users WHERE user_id=?", (session["user_id"],)).fetchone()
                self._event(connection, session["user_id"], "password_changed", "user", session["user_id"],
                            metadata={"previous_revision": int(session["revision"]),
                                      "revision": int(updated["revision"])})
                result = self._user_record(updated)
        if invalid_old_password:
            raise CurrentPasswordIncorrect("当前密码错误")
        if result is None:
            raise AuthenticationFailed("会话不可用")
        return result

    def issue_mcp_credential(
        self, actor: Principal, *, user_id: str | None = None, label: str,
        expires_in_seconds: int | None = None,
    ) -> IssuedCredential:
        target_user_id = user_id or actor.user_id
        clean_label = label.strip() if isinstance(label, str) else ""
        if not clean_label or len(clean_label) > 100:
            raise ValueError("凭据名称必须为1到100个字符")
        if expires_in_seconds is not None and (
            not isinstance(expires_in_seconds, int) or isinstance(expires_in_seconds, bool)
            or not 60 <= expires_in_seconds <= 365 * 24 * 60 * 60
        ):
            raise ValueError("MCP凭据有效期必须为60秒到365天")
        now_dt = self._now()
        now = self._stamp(now_dt)
        expires = self._stamp(now_dt + timedelta(seconds=expires_in_seconds)) if expires_in_seconds else None
        with self._transaction(write=True) as connection:
            current_actor = connection.execute("SELECT * FROM users WHERE user_id=?", (actor.user_id,)).fetchone()
            if current_actor is None or not current_actor["active"]:
                raise Forbidden("当前账号不可用")
            if target_user_id != actor.user_id:
                self._require_admin(connection, actor)
            self._require_active_user(connection, target_user_id)
            target = connection.execute(
                "SELECT must_change_password FROM users WHERE user_id=?", (target_user_id,)
            ).fetchone()
            if target["must_change_password"]:
                raise Conflict("用户必须先完成首次密码修改")
            token, token_hash, token_prefix = issue_token("mcp")
            credential_id = uuid4().hex
            connection.execute(
                "INSERT INTO mcp_credentials(credential_id,token_hash,token_prefix,user_id,label,"
                "created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (credential_id, token_hash, token_prefix, target_user_id, clean_label, now, expires),
            )
            self._event(connection, actor.user_id, "mcp_credential_issued", "credential", credential_id,
                        metadata={"credential_prefix": token_prefix, "user_id": target_user_id})
            return IssuedCredential(credential_id, token, token_prefix, now, expires)

    def resolve_mcp_credential(self, token: str) -> Principal:
        try:
            digest = require_token_kind(token, "mcp")
        except ValueError as exc:
            raise AuthenticationFailed("MCP凭据不可用") from exc
        now_dt = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT u.user_id,u.username,u.role,u.revision,u.active,u.must_change_password,"
                "c.expires_at,c.revoked_at "
                "FROM mcp_credentials c JOIN users u ON u.user_id=c.user_id WHERE c.token_hash=?",
                (digest,),
            ).fetchone()
            if (row is None or row["revoked_at"] is not None or not row["active"]
                    or row["must_change_password"]
                    or (row["expires_at"] and now_dt >= datetime.fromisoformat(row["expires_at"]))):
                raise AuthenticationFailed("MCP凭据不可用")
            return self._principal(row)

    def revoke_mcp_credential(self, actor: Principal, credential_id: str) -> None:
        now = self._stamp(self._now())
        with self._transaction(write=True) as connection:
            current = connection.execute("SELECT * FROM users WHERE user_id=?", (actor.user_id,)).fetchone()
            if current is None or not current["active"]:
                raise Forbidden("当前账号不可用")
            row = connection.execute(
                "SELECT * FROM mcp_credentials WHERE credential_id=?", (credential_id,)
            ).fetchone()
            if row is None:
                raise NotFound("MCP凭据不存在")
            if row["user_id"] != actor.user_id:
                self._require_admin(connection, actor)
            if row["revoked_at"] is None:
                connection.execute("UPDATE mcp_credentials SET revoked_at=? WHERE credential_id=?", (now, credential_id))
                self._event(connection, actor.user_id, "mcp_credential_revoked", "credential", credential_id,
                            metadata={"credential_prefix": row["token_prefix"], "user_id": row["user_id"]})

    def list_mcp_credentials(self, actor: Principal, *, user_id: str | None = None) -> list[dict]:
        target_user_id = user_id or actor.user_id
        with self._transaction() as connection:
            current = connection.execute("SELECT * FROM users WHERE user_id=?", (actor.user_id,)).fetchone()
            if current is None or not current["active"]:
                raise Forbidden("当前账号不可用")
            if target_user_id != actor.user_id:
                self._require_admin(connection, actor)
            rows = connection.execute(
                "SELECT credential_id,token_prefix,user_id,label,created_at,expires_at,revoked_at "
                "FROM mcp_credentials WHERE user_id=? ORDER BY created_at DESC", (target_user_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def revoke_session(self, actor: Principal, session_id: str) -> None:
        now = self._stamp(self._now())
        with self._transaction(write=True) as connection:
            current = connection.execute("SELECT * FROM users WHERE user_id=?", (actor.user_id,)).fetchone()
            if current is None or not current["active"]:
                raise Forbidden("当前账号不可用")
            row = connection.execute("SELECT * FROM web_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                raise NotFound("会话不存在")
            if row["user_id"] != actor.user_id:
                self._require_admin(connection, actor)
            if row["revoked_at"] is None:
                connection.execute("UPDATE web_sessions SET revoked_at=? WHERE session_id=?", (now, session_id))
                self._event(connection, actor.user_id, "web_session_revoked", "session", session_id)

    def synchronize_resource(self, resource_kind: str, resource_id: str, *, active: bool) -> int:
        """仅供可信目录/release组件同步稳定资源，不供普通HTTP路由直接调用。"""
        resource_kind, resource_id = self._resource(resource_kind, resource_id)
        if not isinstance(active, bool):
            raise ValueError("active必须是布尔值")
        now = self._stamp(self._now())
        with self._transaction(write=True) as connection:
            row = connection.execute(
                "SELECT revision FROM resources WHERE resource_kind=? AND resource_id=?",
                (resource_kind, resource_id),
            ).fetchone()
            if row is not None:
                existing = connection.execute(
                    "SELECT active FROM resources WHERE resource_kind=? AND resource_id=?",
                    (resource_kind, resource_id),
                ).fetchone()
                if bool(existing["active"]) is active:
                    return int(row["revision"])
            revision = int(row["revision"]) + 1 if row else 1
            connection.execute(
                "INSERT INTO resources(resource_kind,resource_id,active,revision,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(resource_kind,resource_id) DO UPDATE SET "
                "active=excluded.active,revision=excluded.revision,updated_at=excluded.updated_at",
                (resource_kind, resource_id, int(active), revision, now),
            )
            self._event(connection, None, "resource_catalog_synced", "resource", resource_id,
                        metadata={"resource_kind": resource_kind, "active": active, "revision": revision})
            return revision

    def synchronize_resource_catalog(self, active_resources) -> None:
        """可信目录快照同步：仅激活当前资源并停用消失资源，不创建grant。"""
        normalized = {self._resource(kind, resource_id) for kind, resource_id in active_resources}
        now = self._stamp(self._now())
        with self._transaction(write=True) as connection:
            current = {
                (row["resource_kind"], row["resource_id"]): row
                for row in connection.execute("SELECT * FROM resources")
            }
            for key in sorted(normalized):
                row = current.get(key)
                if row is not None and row["active"]:
                    continue
                revision = int(row["revision"]) + 1 if row else 1
                connection.execute(
                    "INSERT INTO resources(resource_kind,resource_id,active,revision,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(resource_kind,resource_id) DO UPDATE SET "
                    "active=1,revision=excluded.revision,updated_at=excluded.updated_at",
                    (*key, 1, revision, now),
                )
                self._event(connection, None, "resource_catalog_synced", "resource", key[1],
                            metadata={"resource_kind": key[0], "active": True, "revision": revision})
            for key, row in current.items():
                if key in normalized or not row["active"]:
                    continue
                revision = int(row["revision"]) + 1
                connection.execute(
                    "UPDATE resources SET active=0,revision=?,updated_at=? "
                    "WHERE resource_kind=? AND resource_id=?",
                    (revision, now, *key),
                )
                self._event(connection, None, "resource_catalog_synced", "resource", key[1],
                            metadata={"resource_kind": key[0], "active": False, "revision": revision})

    def set_grant(
        self, actor: Principal, *, resource_kind: str, resource_id: str,
        user_id: str, allowed: bool, expected_revision: int,
    ) -> GrantRecord:
        return self._set_grant(
            actor, resource_kind=resource_kind, resource_id=resource_id,
            principal_kind="user", principal_id=user_id, allowed=allowed,
            expected_revision=expected_revision,
        )

    def set_authenticated_grant(
        self, actor: Principal, *, resource_kind: str, resource_id: str,
        allowed: bool, expected_revision: int,
    ) -> GrantRecord:
        """显式授权给全部启用的本地账户；从不放行匿名请求。"""
        return self._set_grant(
            actor, resource_kind=resource_kind, resource_id=resource_id,
            principal_kind="authenticated", principal_id="*", allowed=allowed,
            expected_revision=expected_revision,
        )

    def get_library_classification(self, library_id: str) -> dict | None:
        """读取管理员维护的展示分类，不参与权限判断。"""
        _resource_kind, library_id = self._resource("library", library_id)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT classification,revision "
                "FROM library_classifications WHERE library_id=?",
                (library_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def set_library_classification(
        self, actor: Principal, *, library_id: str, classification: str,
        expected_revision: int,
    ) -> dict:
        """CAS更新展示分类并写审计；不创建/修改任何grant。"""
        _resource_kind, library_id = self._resource("library", library_id)
        if not isinstance(classification, str) or classification not in {"public", "private"}:
            raise ValueError("资料库分类只能是public或private")
        if (not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
                or expected_revision < 0):
            raise ValueError("expected_revision必须是非负整数")
        now = self._stamp(self._now())
        with self._transaction(write=True) as connection:
            admin = self._require_admin(connection, actor)
            row = connection.execute(
                "SELECT classification,revision FROM library_classifications WHERE library_id=?",
                (library_id,),
            ).fetchone()
            current_revision = int(row["revision"]) if row else 0
            if current_revision != expected_revision:
                raise Conflict("资料库分类已变化，请刷新后重试")
            revision = current_revision + 1
            connection.execute(
                "INSERT INTO library_classifications(library_id,classification,revision,updated_at,updated_by) "
                "VALUES(?,?,?,?,?) ON CONFLICT(library_id) DO UPDATE SET "
                "classification=excluded.classification,revision=excluded.revision,"
                "updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                (library_id, classification, revision, now, admin["user_id"]),
            )
            self._event(
                connection, admin["user_id"], "library_classification_changed", "library", library_id,
                metadata={
                    "classification": classification,
                    "previous_classification": row["classification"] if row else None,
                    "previous_revision": current_revision,
                    "revision": revision,
                },
            )
            return {
                "library_id": library_id,
                "classification": classification,
                "revision": revision,
                "updated_at": now,
                "updated_by": admin["user_id"],
            }

    def _set_grant(
        self, actor: Principal, *, resource_kind: str, resource_id: str,
        principal_kind: str, principal_id: str, allowed: bool, expected_revision: int,
    ) -> GrantRecord:
        resource_kind, resource_id = self._resource(resource_kind, resource_id)
        if not isinstance(allowed, bool) or expected_revision < 0:
            raise ValueError("授权状态或revision无效")
        now = self._stamp(self._now())
        with self._transaction(write=True) as connection:
            admin = self._require_admin(connection, actor)
            resource = connection.execute(
                "SELECT active FROM resources WHERE resource_kind=? AND resource_id=?",
                (resource_kind, resource_id),
            ).fetchone()
            if resource is None or not resource["active"]:
                raise NotFound("稳定资料库资源不存在或未处于活动状态")
            if principal_kind == "user":
                self._require_active_user(connection, principal_id)
            row = connection.execute(
                "SELECT * FROM resource_grants WHERE resource_kind=? AND resource_id=? "
                "AND principal_kind=? AND principal_id=?",
                (resource_kind, resource_id, principal_kind, principal_id),
            ).fetchone()
            current_revision = int(row["revision"]) if row else 0
            if current_revision != expected_revision:
                raise Conflict("授权记录已变化，请刷新后重试")
            next_revision = current_revision + 1
            connection.execute(
                "INSERT INTO resource_grants(resource_kind,resource_id,principal_kind,principal_id,"
                "allowed,revision,updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(resource_kind,resource_id,principal_kind,principal_id) DO UPDATE SET "
                "allowed=excluded.allowed,revision=excluded.revision,updated_at=excluded.updated_at,"
                "updated_by=excluded.updated_by",
                (resource_kind, resource_id, principal_kind, principal_id,
                 int(allowed), next_revision, now, admin["user_id"]),
            )
            self._event(
                connection, admin["user_id"], "resource_grant_changed", "resource", resource_id,
                metadata={"resource_kind": resource_kind, "active": allowed, "allowed": allowed,
                          "previous_allowed": bool(row["allowed"]) if row else None,
                          "previous_revision": current_revision, "revision": next_revision,
                          "principal_kind": principal_kind, "principal_id": principal_id},
            )
            return GrantRecord(resource_kind, resource_id, principal_kind, principal_id,
                               allowed, next_revision, now, admin["user_id"])

    def load_access_snapshot(self, principal: Principal) -> "AccessSnapshot":
        """每请求加载当前账号、资源和grant快照；未知/停用资源默认无权。"""
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM users WHERE user_id=?", (principal.user_id,)).fetchone()
            if row is None or not row["active"] or row["must_change_password"]:
                raise AuthenticationFailed("账号不可用")
            current = self._principal(row)
            resources = frozenset(
                (item["resource_kind"], item["resource_id"])
                for item in connection.execute("SELECT resource_kind,resource_id FROM resources WHERE active=1")
            )
            grants = {
                (item["resource_kind"], item["resource_id"], item["principal_kind"]): bool(item["allowed"])
                for item in connection.execute(
                    "SELECT resource_kind,resource_id,principal_kind,allowed FROM resource_grants "
                    "WHERE principal_id IN (?, '*')", (current.user_id,),
                )
            }
            return AccessSnapshot(current, resources, grants)

    def can_read(self, principal: Principal, resource_kind: str, resource_id: str) -> bool:
        resource_kind, resource_id = self._resource(resource_kind, resource_id)
        return self.load_access_snapshot(principal).allows(resource_kind, resource_id)

    def can_read_managed_file(self, principal: Principal, library_id: str, file_id: str) -> bool:
        return self.load_access_snapshot(principal).allows_managed_file(library_id, file_id)

    def list_resources(self, actor: Principal) -> list[dict]:
        with self._transaction() as connection:
            self._require_admin(connection, actor)
            return [dict(row) for row in connection.execute(
                "SELECT resource_kind,resource_id,active,revision,updated_at FROM resources "
                "ORDER BY resource_kind,resource_id"
            )]

    def list_grants(
        self, actor: Principal, *, resource_kind: str | None = None,
        resource_id: str | None = None, user_id: str | None = None,
    ) -> list[GrantRecord]:
        clauses, values = [], []
        if resource_kind is not None:
            if resource_kind not in RESOURCE_KINDS:
                raise ValueError("资料库资源类型无效")
            clauses.append("g.resource_kind=?")
            values.append(resource_kind)
        if resource_id is not None:
            if not _RESOURCE_ID_RE.fullmatch(resource_id):
                raise ValueError("稳定资源ID格式无效")
            clauses.append("g.resource_id=?")
            values.append(resource_id)
        if user_id is not None:
            clauses.append("g.principal_id=?")
            values.append(user_id)
        with self._transaction() as connection:
            self._require_admin(connection, actor)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = connection.execute(
                "SELECT g.* FROM resource_grants g" + where +
                " ORDER BY g.resource_kind,g.resource_id,g.principal_kind,g.principal_id", values,
            ).fetchall()
            return [GrantRecord(
                item["resource_kind"], item["resource_id"], item["principal_kind"],
                item["principal_id"], bool(item["allowed"]), int(item["revision"]),
                item["updated_at"], item["updated_by"],
            ) for item in rows]

    def list_audit(
        self, actor: Principal, *, limit: int = 100, before_event_id: str | None = None,
    ) -> list[dict]:
        if not 1 <= limit <= 200:
            raise ValueError("审计分页limit必须是1到200之间")
        with self._transaction() as connection:
            self._require_admin(connection, actor)
            if before_event_id:
                cursor = connection.execute(
                    "SELECT occurred_at,event_id FROM audit_events WHERE event_id=?", (before_event_id,)
                ).fetchone()
                if cursor is None:
                    raise NotFound("审计游标不存在")
                rows = connection.execute(
                    "SELECT * FROM audit_events WHERE (occurred_at,event_id)<(?,?) "
                    "ORDER BY occurred_at DESC,event_id DESC LIMIT ?",
                    (cursor["occurred_at"], cursor["event_id"], limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM audit_events ORDER BY occurred_at DESC,event_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            import json
            return [{
                "event_id": row["event_id"], "occurred_at": row["occurred_at"],
                "actor_user_id": row["actor_user_id"], "action": row["action"],
                "target_kind": row["target_kind"], "target_id": row["target_id"],
                "outcome": row["outcome"], "request_id": row["request_id"],
                "metadata": json.loads(row["metadata_json"]),
            } for row in rows]


@dataclass(frozen=True)
class AccessSnapshot:
    principal: Principal
    active_resources: frozenset[tuple[str, str]]
    grants: dict[tuple[str, str, str], bool]

    def allows(self, resource_kind: str, resource_id: str) -> bool:
        key = (resource_kind, resource_id)
        if key not in self.active_resources:
            return False
        if self.principal.role == "admin":
            return True
        specific = self.grants.get((resource_kind, resource_id, "user"))
        if specific is not None:
            # 明确拒绝优先于“所有已认证账户”授权。
            return specific
        return self.grants.get((resource_kind, resource_id, "authenticated"), False)

    def allows_managed_file(self, library_id: str, file_id: str) -> bool:
        """文件精确allow/deny优先；缺少文件规则才继承库授权。"""
        if ("managed_file", file_id) not in self.active_resources:
            return False
        if self.principal.role == "admin":
            return True
        for principal_kind in ("user", "authenticated"):
            exact = self.grants.get(("managed_file", file_id, principal_kind))
            if exact is not None:
                return exact
        for kind in ("managed_library", "library"):
            if (kind, library_id) not in self.active_resources:
                continue
            specific = self.grants.get((kind, library_id, "user"))
            if specific is not None:
                return specific
            inherited = self.grants.get((kind, library_id, "authenticated"))
            if inherited is not None:
                return inherited
        return False
