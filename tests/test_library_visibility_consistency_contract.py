"""窄化验证资料库分类元数据的身份门禁与权限隔离。"""

from __future__ import annotations

import pytest

from wenjin.identity import AuthenticationFailed, Forbidden, IdentityStore

pytestmark = pytest.mark.quick

_ADMIN_PASSWORD = "Visibility_Admin_Password_2026!"
_TEMP_PASSWORD = "Visibility_Temporary_Password_2026!"
_READY_PASSWORD = "Visibility_Ready_Password_2026!"


def test_reader_disabled_and_must_change_admin_cannot_classify(tmp_path):
    store = IdentityStore((tmp_path / "identity.sqlite").resolve())
    store.initialize()
    store.bootstrap_first_admin("visibility-root", _ADMIN_PASSWORD)
    admin = store.login("visibility-root", _ADMIN_PASSWORD).principal

    reader_record = store.create_user(admin, "visibility-reader", _TEMP_PASSWORD)
    reader_login = store.login(reader_record.username, _TEMP_PASSWORD)
    store.change_password(reader_login.token, _TEMP_PASSWORD, _READY_PASSWORD)
    reader = store.login(reader_record.username, _READY_PASSWORD).principal

    must_change_admin_record = store.create_user(
        admin, "visibility-temp-admin", _TEMP_PASSWORD, role="admin"
    )
    must_change_admin = store.login(must_change_admin_record.username, _TEMP_PASSWORD).principal

    store.synchronize_resource("library", "visibility-library", active=True)
    for principal in (reader, must_change_admin):
        with pytest.raises(Forbidden):
            store.set_library_classification(
                principal,
                library_id="visibility-library",
                classification="public",
                expected_revision=0,
            )

    ready_admin_login = store.change_password(
        store.login(must_change_admin_record.username, _TEMP_PASSWORD).token,
        _TEMP_PASSWORD,
        _READY_PASSWORD,
    )
    ready_admin = store.login(must_change_admin_record.username, _READY_PASSWORD).principal
    disabled_record = store.set_user_active(
        admin,
        ready_admin.user_id,
        False,
        expected_revision=ready_admin.revision,
    )
    del disabled_record, ready_admin_login

    # 已停用管理员持有的旧 Principal 也不能继续写分类。
    with pytest.raises(Forbidden):
        store.set_library_classification(
            ready_admin,
            library_id="visibility-library",
            classification="public",
            expected_revision=0,
        )

    assert store.get_library_classification("visibility-library") is None
    assert store.list_grants(admin, resource_kind="library", resource_id="visibility-library") == []


def test_admin_access_snapshot_reloads_current_account_status_and_role(tmp_path):
    store = IdentityStore((tmp_path / "identity.sqlite").resolve())
    store.initialize()
    store.bootstrap_first_admin("snapshot-root", _ADMIN_PASSWORD)
    root_admin = store.login("snapshot-root", _ADMIN_PASSWORD).principal
    store.synchronize_resource_catalog({("library", "snapshot-active")})

    demoted_record = store.create_user(root_admin, "snapshot-demoted", _TEMP_PASSWORD, role="admin")
    demoted_first_login = store.login(demoted_record.username, _TEMP_PASSWORD)
    store.change_password(demoted_first_login.token, _TEMP_PASSWORD, _READY_PASSWORD)
    demoted_admin = store.login(demoted_record.username, _READY_PASSWORD).principal
    assert store.can_read(demoted_admin, "library", "snapshot-active") is True
    store.set_user_role(
        root_admin,
        demoted_admin.user_id,
        "reader",
        expected_revision=demoted_admin.revision,
    )
    # 旧Principal不能缓存旧admin身份，下一次snapshot必须读取当前role。
    assert store.can_read(demoted_admin, "library", "snapshot-active") is False

    disabled_record = store.create_user(
        root_admin, "snapshot-disabled", _TEMP_PASSWORD, role="admin"
    )
    disabled_first_login = store.login(disabled_record.username, _TEMP_PASSWORD)
    store.change_password(disabled_first_login.token, _TEMP_PASSWORD, _READY_PASSWORD)
    disabled_admin = store.login(disabled_record.username, _READY_PASSWORD).principal
    store.set_user_active(
        root_admin,
        disabled_admin.user_id,
        False,
        expected_revision=disabled_admin.revision,
    )
    with pytest.raises(AuthenticationFailed, match="账号不可用"):
        store.can_read(disabled_admin, "library", "snapshot-active")

    pending_record = store.create_user(root_admin, "snapshot-pending", _TEMP_PASSWORD, role="admin")
    pending_admin = store.login(pending_record.username, _TEMP_PASSWORD).principal
    with pytest.raises(AuthenticationFailed, match="账号不可用"):
        store.can_read(pending_admin, "library", "snapshot-active")

    assert store.can_read(root_admin, "library", "snapshot-inactive") is False
