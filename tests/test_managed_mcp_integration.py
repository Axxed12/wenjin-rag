"""Managed MCP ACL integration contracts that can run without starting services."""

from __future__ import annotations

from pathlib import Path

import pytest

from wenjin.access import AccessPolicy, AccessRegistry, AccessSnapshotError, load_snapshot
from wenjin.managed_mcp import ManagedFile, require_managed_file_access, visible_managed_files

FILE_A = "a" * 32
FILE_B = "b" * 32
AUTHORIZED = "managed-mcp-authorized"


class ListedFiles:
    """Small bridge stand-in exposing the production listing contract."""

    def __init__(self, *file_ids: str) -> None:
        self.files = [
            ManagedFile(
                file_id=file_id,
                display_name=f"{file_id[0]}-private-guide.txt",
                version="1",
                version_id=f"version-{file_id[0]}",
                chunk_count=1,
                updated_at="2026-10-06T00:00:00+00:00",
            )
            for file_id in file_ids
        ]

    def list_files(self) -> list[ManagedFile]:
        return self.files


def registry(*policies: AccessPolicy) -> AccessRegistry:
    return AccessRegistry(Path("unused"), {policy.corpus_id: policy for policy in policies})


def internal(corpus_id: str, clients: tuple[str, ...]) -> AccessPolicy:
    return AccessPolicy(corpus_id, "internal", clients)


def test_managed_file_acl_missing_defaults_to_deny_and_hides_list() -> None:
    access = registry()
    bridge = ListedFiles(FILE_A, FILE_B)

    assert visible_managed_files(bridge, access, AUTHORIZED) == []
    with pytest.raises(PermissionError):
        require_managed_file_access(access, FILE_A, AUTHORIZED)


def test_internal_group_whitelist_allows_named_client_but_denies_anonymous() -> None:
    access = registry(internal("managed-files", (AUTHORIZED,)))
    bridge = ListedFiles(FILE_A)

    assert [item.file_id for item in visible_managed_files(bridge, access, AUTHORIZED)] == [FILE_A]
    assert visible_managed_files(bridge, access, None) == []
    with pytest.raises(PermissionError):
        require_managed_file_access(access, FILE_A, None)


def test_file_specific_deny_overrides_group_allow() -> None:
    access = registry(
        internal("managed-files", (AUTHORIZED,)),
        internal(f"managed-file-{FILE_A}", ()),
    )
    bridge = ListedFiles(FILE_A, FILE_B)

    assert [item.file_id for item in visible_managed_files(bridge, access, AUTHORIZED)] == [FILE_B]
    with pytest.raises(PermissionError):
        require_managed_file_access(access, FILE_A, AUTHORIZED)


def test_only_explicit_public_wildcard_allows_anonymous() -> None:
    public = AccessPolicy("managed-files", "public", ("*",))
    assert [item.file_id for item in visible_managed_files(ListedFiles(FILE_A), registry(public), None)] == [FILE_A]

    unsafe_internal = internal("managed-files", ("*",))
    with pytest.raises(PermissionError):
        require_managed_file_access(registry(unsafe_internal), FILE_A, AUTHORIZED)


def test_corrupt_access_snapshot_fails_closed(tmp_path: Path) -> None:
    access_dir = tmp_path / "access"
    access_dir.mkdir()
    (access_dir / "managed-files.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(AccessSnapshotError):
        load_snapshot(access_dir)

