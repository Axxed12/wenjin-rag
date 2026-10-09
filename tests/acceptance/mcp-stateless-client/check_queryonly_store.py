"""Assert the stateless identity adapter is read-only inside its runtime image."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from wenjin.mcp.stateless_identity import StatelessIdentityStore


def main() -> None:
    store = StatelessIdentityStore(Path("/var/lib/wenjin/identity.sqlite"))
    initialize_rejected = False
    try:
        store.initialize()
    except PermissionError:
        initialize_rejected = True
    with store._connect() as connection:
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        try:
            connection.execute("UPDATE users SET active=active WHERE 1=0")
            write_rejected = False
        except sqlite3.OperationalError as error:
            write_rejected = "readonly" in str(error).lower() or "query only" in str(error).lower()
    result = {"initializeRejected": initialize_rejected, "queryOnly": query_only,
              "writeRejected": write_rejected}
    print(json.dumps(result, sort_keys=True))
    if result != {"initializeRejected": True, "queryOnly": 1, "writeRejected": True}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
