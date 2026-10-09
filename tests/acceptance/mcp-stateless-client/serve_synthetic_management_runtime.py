"""Temporary real management app with synthetic Qdrant/model/identity assets."""

import os
from pathlib import Path

import uvicorn

from wenjin.management_http.app import create_app
from wenjin.settings import Settings


def main() -> None:
    root = Path("/app/management-project")
    root.mkdir(parents=True, exist_ok=True)
    settings = Settings.for_project_root(root)
    app = create_app(
        root,
        settings=settings,
        identity_db=Path("/var/lib/management/identity.sqlite"),
    )
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8777,
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
