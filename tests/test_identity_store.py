"""Identity core contracts use only an explicit temporary SQLite database."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

import wenjin.identity.store as identity_store_module
from wenjin.identity import AuthenticationFailed, Conflict, Forbidden, IdentityStore

pytestmark = pytest.mark.quick

ADMIN_PASSWORD = "Local_Admin_Password_2026!"
READER_PASSWORD = "Local_Reader_Password_2026!"
CHANGED_PASSWORD = "Reader_Changed_Password_2026!"


def make_store(path, *, idle_seconds: int = 60, absolute_seconds: int = 300) -> IdentityStore:
    """创建只指向调用方临时目录的store，不使用settings或默认身份库。"""
    store = IdentityStore(
        path.resolve(),
        session_idle_seconds=idle_seconds,
        session_absolute_seconds=absolute_seconds,
    )
    store.initialize()
    return store


def bootstrap_admin(store: IdentityStore, username: str = "rootadmin"):
    store.bootstrap_first_admin(username, ADMIN_PASSWORD)
    return store.login(username, ADMIN_PASSWORD).principal


def create_ready_user(
    store: IdentityStore, actor, *, username: str = "reader1", role: str = "reader"
):
    store.create_user(actor, username, READER_PASSWORD, role=role)
    first_session = store.login(username, READER_PASSWORD)
    assert first_session.must_change_password is True
    store.change_password(first_session.token, READER_PASSWORD, CHANGED_PASSWORD)
    return store.login(username, CHANGED_PASSWORD).principal


def test_bootstrap_first_admin_is_atomic_under_two_store_instances(tmp_path):
    path = tmp_path / "identity.sqlite"
    left = make_store(path)
    right = IdentityStore(path.resolve())
    right.initialize()
    start = threading.Barrier(2)

    def bootstrap(store: IdentityStore, username: str):
        start.wait(timeout=5)
        try:
            return ("created", store.bootstrap_first_admin(username, ADMIN_PASSWORD))
        except Conflict:
            return ("closed", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda item: bootstrap(*item),
            [(left, "root-one"), (right, "root-two")],
        ))

    assert sorted(result for result, _record in results) == ["closed", "created"]
    created = next(record for result, record in results if result == "created")
    assert left.login(created.username, ADMIN_PASSWORD).principal.user_id == created.user_id
    assert len(left.list_users(left.login(created.username, ADMIN_PASSWORD).principal)) == 1


def test_first_login_requires_password_change_before_admin_actions(tmp_path):
    store = make_store(tmp_path / "identity.sqlite")
    actor = bootstrap_admin(store)
    reader = create_ready_user(store, actor)
    with pytest.raises(Forbidden):
        store.list_users(reader)

    temporary_admin = store.create_user(
        actor, "tempadmin", READER_PASSWORD, role="admin"
    )
    first = store.login(temporary_admin.username, READER_PASSWORD)
    assert first.must_change_password is True
    assert first.principal.must_change_password is True
    with pytest.raises(Forbidden):
        store.list_users(first.principal)

    updated = store.change_password(first.token, READER_PASSWORD, CHANGED_PASSWORD)
    assert updated.must_change_password is False
    with pytest.raises(AuthenticationFailed):
        store.resolve_session(first.token)
    changed = store.login(temporary_admin.username, CHANGED_PASSWORD)
    assert changed.must_change_password is False
    assert len(store.list_users(changed.principal)) == 3


def test_web_and_mcp_tokens_are_separate_and_both_expire(tmp_path, monkeypatch):
    store = make_store(
        tmp_path / "identity.sqlite", idle_seconds=60, absolute_seconds=300
    )
    actor = bootstrap_admin(store)
    session = store.login("rootadmin", ADMIN_PASSWORD)
    credential = store.issue_mcp_credential(actor, label="test-client", expires_in_seconds=60)

    with pytest.raises(ValueError, match="用途不匹配"):
        store.resolve_mcp_credential(session.token)
    with pytest.raises(ValueError, match="用途不匹配"):
        store.resolve_session(credential.token)

    original_now = store._now()
    monkeypatch.setattr(store, "_now", lambda: original_now + timedelta(seconds=61))
    with pytest.raises(AuthenticationFailed):
        store.resolve_session(session.token)
    with pytest.raises(AuthenticationFailed):
        store.resolve_mcp_credential(credential.token)


def test_password_reset_and_disable_revoke_existing_sessions_and_mcp_tokens(tmp_path):
    store = make_store(tmp_path / "identity.sqlite")
    admin = bootstrap_admin(store)
    user = create_ready_user(store, admin)
    user_session = store.login(user.username, CHANGED_PASSWORD)
    mcp = store.issue_mcp_credential(user, label="revocation-check")

    reset = store.reset_password(
        admin, user.user_id, READER_PASSWORD, expected_revision=user.revision
    )
    assert reset.must_change_password is True
    with pytest.raises(AuthenticationFailed):
        store.resolve_session(user_session.token)
    with pytest.raises(AuthenticationFailed):
        store.resolve_mcp_credential(mcp.token)

    reset_session = store.login(user.username, READER_PASSWORD)
    store.change_password(reset_session.token, READER_PASSWORD, CHANGED_PASSWORD)
    fresh_user = store.login(user.username, CHANGED_PASSWORD).principal
    fresh_session = store.login(user.username, CHANGED_PASSWORD)
    fresh_mcp = store.issue_mcp_credential(fresh_user, label="disable-check")
    disabled = store.set_user_active(
        admin, user.user_id, False, expected_revision=fresh_user.revision
    )
    assert disabled.active is False
    with pytest.raises(AuthenticationFailed):
        store.resolve_session(fresh_session.token)
    with pytest.raises(AuthenticationFailed):
        store.resolve_mcp_credential(fresh_mcp.token)


def test_explicit_user_deny_overrides_authenticated_allow_and_two_stores_refresh(tmp_path):
    path = tmp_path / "identity.sqlite"
    first = make_store(path)
    second = IdentityStore(path.resolve())
    admin = bootstrap_admin(first)
    reader = create_ready_user(first, admin)
    first.synchronize_resource("library", "library-alpha", active=True)
    first.set_authenticated_grant(
        admin,
        resource_kind="library",
        resource_id="library-alpha",
        allowed=True,
        expected_revision=0,
    )
    assert second.can_read(reader, "library", "library-alpha") is True
    assert second.can_read(reader, "library", "library-unknown") is False

    first.set_grant(
        admin,
        resource_kind="library",
        resource_id="library-alpha",
        user_id=reader.user_id,
        allowed=False,
        expected_revision=0,
    )
    assert second.can_read(reader, "library", "library-alpha") is False
    first.set_grant(
        admin,
        resource_kind="library",
        resource_id="library-alpha",
        user_id=reader.user_id,
        allowed=True,
        expected_revision=1,
    )
    assert second.can_read(reader, "library", "library-alpha") is True

    first.synchronize_resource("library", "library-alpha", active=False)
    assert second.can_read(reader, "library", "library-alpha") is False


def test_grant_revision_cas_and_self_lockout_protection(tmp_path):
    store = make_store(tmp_path / "identity.sqlite")
    admin = bootstrap_admin(store)
    reader = create_ready_user(store, admin)
    store.synchronize_resource("library", "library-beta", active=True)
    grant = store.set_grant(
        admin,
        resource_kind="library",
        resource_id="library-beta",
        user_id=reader.user_id,
        allowed=True,
        expected_revision=0,
    )
    assert grant.revision == 1
    with pytest.raises(Conflict, match="授权记录已变化"):
        store.set_grant(
            admin,
            resource_kind="library",
            resource_id="library-beta",
            user_id=reader.user_id,
            allowed=False,
            expected_revision=0,
        )

    refreshed_admin = store.login("rootadmin", ADMIN_PASSWORD).principal
    with pytest.raises(Conflict):
        store.set_user_role(
            refreshed_admin,
            refreshed_admin.user_id,
            "reader",
            expected_revision=refreshed_admin.revision,
        )
    with pytest.raises(Conflict):
        store.set_user_active(
            refreshed_admin,
            refreshed_admin.user_id,
            False,
            expected_revision=refreshed_admin.revision,
        )
    users = store.list_users(refreshed_admin)
    root_record = next(user for user in users if user.user_id == refreshed_admin.user_id)
    assert root_record.active is True and root_record.role == "admin"


def test_user_change_and_audit_append_share_one_transaction(tmp_path, monkeypatch):
    store = make_store(tmp_path / "identity.sqlite")
    admin = bootstrap_admin(store)
    before_actions = [item["action"] for item in store.list_audit(admin)]
    calls = 0

    def fail_audit(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("injected audit write failure")

    monkeypatch.setattr(identity_store_module, "append_event", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit write failure"):
        store.create_user(admin, "must-rollback", READER_PASSWORD)
    assert calls == 1, "故障注入必须命中IdentityStore实际调用的append_event符号"
    assert [user.username for user in store.list_users(admin)] == ["rootadmin"]
    assert [item["action"] for item in store.list_audit(admin)] == before_actions


def test_issued_secret_is_not_repr_list_or_audit_output(tmp_path):
    store = make_store(tmp_path / "identity.sqlite")
    admin = bootstrap_admin(store)
    session = store.login("rootadmin", ADMIN_PASSWORD)
    credential = store.issue_mcp_credential(admin, label="audit-safe")
    listed = store.list_mcp_credentials(admin)
    audit = store.list_audit(admin)
    public_text = "\n".join((repr(session), repr(credential), json.dumps(listed), json.dumps(audit)))

    assert session.token not in public_text
    assert credential.token not in public_text
    assert ADMIN_PASSWORD not in public_text
    assert READER_PASSWORD not in public_text
    assert all("token" not in item and "secret" not in item for item in listed)


def test_reader_can_revoke_only_own_credentials_and_logout_is_scoped(tmp_path):
    store = make_store(tmp_path / "identity.sqlite")
    admin = bootstrap_admin(store)
    reader = create_ready_user(store, admin)
    admin_credential = store.issue_mcp_credential(admin, label="admin-owned")
    reader_credential = store.issue_mcp_credential(reader, label="reader-owned")
    other_reader_credential = store.issue_mcp_credential(
        reader, label="reader-owned-keep")
    reader_session = store.login(reader.username, CHANGED_PASSWORD)
    other_reader_session = store.login(reader.username, CHANGED_PASSWORD)

    with pytest.raises(Forbidden):
        store.revoke_mcp_credential(reader, admin_credential.credential_id)
    assert store.resolve_mcp_credential(admin_credential.token).user_id == admin.user_id

    store.revoke_mcp_credential(reader, reader_credential.credential_id)
    with pytest.raises(AuthenticationFailed):
        store.resolve_mcp_credential(reader_credential.token)
    assert store.resolve_mcp_credential(other_reader_credential.token).user_id == reader.user_id

    store.logout(reader_session.token)
    with pytest.raises(AuthenticationFailed):
        store.resolve_session(reader_session.token)
    assert store.resolve_session(other_reader_session.token).user_id == reader.user_id

    audit = store.list_audit(admin, limit=200)
    issued = next(
        event for event in audit
        if event["action"] == "mcp_credential_issued"
        and event["target_id"] == reader_credential.credential_id
    )
    revoked = next(
        event for event in audit
        if event["action"] == "mcp_credential_revoked"
        and event["target_id"] == reader_credential.credential_id
    )
    logged_out = next(
        event for event in audit
        if event["action"] == "logout"
        and event["actor_user_id"] == reader.user_id
    )
    assert issued["actor_user_id"] == reader.user_id
    assert issued["metadata"]["user_id"] == reader.user_id
    assert revoked["actor_user_id"] == reader.user_id
    assert revoked["metadata"]["user_id"] == reader.user_id
    assert logged_out["actor_user_id"] == reader.user_id
    assert logged_out["target_kind"] == "session"
    assert logged_out["target_id"]


def test_grant_audit_identifies_actor_principal_resource_and_transition(tmp_path):
    store = make_store(tmp_path / "identity.sqlite")
    admin = bootstrap_admin(store)
    reader = create_ready_user(store, admin)
    store.synchronize_resource("library", "library-audit-contract", active=True)
    initial = store.set_grant(
        admin,
        resource_kind="library",
        resource_id="library-audit-contract",
        user_id=reader.user_id,
        allowed=True,
        expected_revision=0,
    )
    changed = store.set_grant(
        admin,
        resource_kind="library",
        resource_id="library-audit-contract",
        user_id=reader.user_id,
        allowed=False,
        expected_revision=initial.revision,
    )

    event = next(
        item for item in store.list_audit(admin, limit=200)
        if item["action"] == "resource_grant_changed"
        and item["target_id"] == "library-audit-contract"
        and item["metadata"].get("principal_id") == reader.user_id
    )
    assert event["actor_user_id"] == admin.user_id
    assert event["target_kind"] == "resource"
    assert event["target_id"] == "library-audit-contract"
    assert event["metadata"] == {
        "active": False,
        "allowed": False,
        "previous_allowed": True,
        "previous_revision": 1,
        "principal_id": reader.user_id,
        "principal_kind": "user",
        "revision": 2,
        "resource_kind": "library",
    }
    assert changed.revision == 2
