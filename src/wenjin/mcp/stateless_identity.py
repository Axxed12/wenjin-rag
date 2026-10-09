"""Read-only IdentityStore adapter for the stateless MCP process.

SQLite WAL readers may need to create/update ``-wal`` and ``-shm`` sidecars even
when they never change identity rows. The deployment therefore grants this
process filesystem write access to the identity volume for SQLite coordination,
while every connection is put into SQLite query-only mode and schema
initialization is disabled.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from wenjin.identity.store import IdentityStore


class StatelessIdentityStore(IdentityStore):
    """Allow fresh credential/grant reads but reject database mutations."""

    def initialize(self) -> None:
        raise PermissionError("stateless MCP身份库只读，禁止initialize")

    @contextmanager
    def _connect(self, *, allow_create: bool = False) -> Iterator[sqlite3.Connection]:
        if allow_create:
            raise PermissionError("stateless MCP身份库只读，禁止创建数据库")
        with super()._connect(allow_create=False) as connection:
            connection.execute("PRAGMA query_only=ON")
            if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise RuntimeError("无法为stateless MCP身份连接启用query_only")
            yield connection
