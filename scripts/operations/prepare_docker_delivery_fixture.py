"""Prepare a disposable, offline-buildable Docker integration fixture.

This command only stages selected source and creates temporary synthetic secrets.
It does not invoke Docker unless the explicit --build switch is supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path
from uuid import uuid4

BASE_IMAGE = "wenjin-backend:2026-10-14"
BASE_IMAGE_ID = "sha256:6941765e2224efa160aa01e5a2518563d6c2bd9f3b93a2940ce33a90215d37cd"
FIXTURE = Path("tests/fixtures/team_delivery/synthetic_model_api.py")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _source_files(root: Path):
    for folder in ("src", "scripts"):
        base = root / folder
        if not base.is_dir():
            raise ValueError(f"required source directory missing: {folder}")
        for item in sorted(base.rglob("*")):
            if item.is_symlink():
                raise ValueError(f"source contains a symbolic link: {item.relative_to(root)}")
            if not item.is_file() or "__pycache__" in item.parts or item.suffix == ".pyc":
                continue
            if item.name.casefold().startswith(".env") or any(
                marker in item.name.casefold() for marker in ("credential", "secret", "private-key")
            ):
                raise ValueError(f"sensitive-looking file refused from build context: {item.relative_to(root)}")
            yield item


def prepare(
    source_root: Path,
    output: Path,
    *,
    expected_source_sha256: str | None = None,
    build: bool = False,
) -> dict:
    source_root = source_root.resolve(strict=True)
    output = output.absolute()
    if not source_root.is_dir() or output.exists() or output.is_symlink():
        raise ValueError("source_root must be a directory and output must not exist")
    if output.resolve().is_relative_to(source_root):
        raise ValueError("fixture output must be outside the source checkout")

    run_id = uuid4().hex
    image_tag = f"wenjin-backend:team-delivery-{run_id[:12]}"
    output.mkdir(parents=True)
    rows = []
    try:
        for source in _source_files(source_root):
            relative = source.relative_to(source_root)
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            rows.append({
                "path": relative.as_posix(),
                "size_bytes": target.stat().st_size,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            })
        fixture_source = source_root / FIXTURE
        fixture_target = output / "delivery-fixture" / "synthetic_model_api.py"
        fixture_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fixture_source, fixture_target)
        rows.append({
            "path": "delivery-fixture/synthetic_model_api.py",
            "size_bytes": fixture_target.stat().st_size,
            "sha256": hashlib.sha256(fixture_target.read_bytes()).hexdigest(),
        })
        canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        source_sha256 = hashlib.sha256(canonical).hexdigest()
        if expected_source_sha256 and source_sha256 != expected_source_sha256.lower():
            raise ValueError("staged source SHA256 does not match the supplied freeze hash")

        dockerfile = f"""FROM {BASE_IMAGE}
LABEL com.wenjin.delivery.run_id=\"{run_id}\"
LABEL com.wenjin.delivery.source_sha256=\"{source_sha256}\"
COPY src/ /app/src/
COPY scripts/ /app/scripts/
COPY delivery-fixture/synthetic_model_api.py /app/delivery-fixture/synthetic_model_api.py
ENV PYTHONPATH=/app/src
"""
        (output / "Dockerfile.delivery").write_text(dockerfile, encoding="utf-8")
        (output / ".dockerignore").write_text(
            "secrets/\n.env\ncompose.synthetic.yaml\nfixture-manifest.json\n",
            encoding="utf-8",
        )
        compose = f"""name: team-delivery-{run_id[:12]}
services:
  qdrant:
    image: qdrant/qdrant:v1.19.1
    labels:
      com.wenjin.delivery.run_id: \"{run_id}\"
      com.wenjin.delivery.source_sha256: \"{source_sha256}\"
    volumes:
      - qdrant-origin:/qdrant/storage
    expose: [\"6333\"]
    networks: [isolated]
  synthetic-model:
    image: {image_tag}
    entrypoint: [python, /app/delivery-fixture/synthetic_model_api.py]
    environment:
      EMBEDDING_MODEL_ID: delivery-synthetic-embed-v1
      RERANKER_MODEL_ID: delivery-synthetic-rerank-v1
    secrets: [model_api_token]
    labels:
      com.wenjin.delivery.run_id: \"{run_id}\"
      com.wenjin.delivery.source_sha256: \"{source_sha256}\"
    networks: [isolated]
  management:
    image: {image_tag}
    command: [python, -m, wenjin.file_management, serve, --root, /app, --host, 0.0.0.0, --port, \"8777\", --identity-db, /app/data/file-management/manual-agent-identity.sqlite]
    environment: &app-env
      QDRANT_URL: http://qdrant:6333
      QDRANT_API_KEY: \"\"
      WENJIN_PROJECT_ROOT: /app
      WENJIN_IDENTITY_DB: /app/data/file-management/manual-agent-identity.sqlite
      WENJIN_MANAGED_RAW_DATA_ROOT: data/file-management/raw_data
      WENJIN_MODEL_PROVIDER: remote
      WENJIN_MODEL_API_URL: http://synthetic-model:8765
      EMBEDDING_MODEL_ID: delivery-synthetic-embed-v1
      RERANKER_MODEL_ID: delivery-synthetic-rerank-v1
      WENJIN_MODEL_API_TIMEOUT_SECONDS: \"45\"
      WENJIN_MODEL_API_CONNECT_TIMEOUT_SECONDS: \"5\"
      WENJIN_MODEL_API_POOL_TIMEOUT_SECONDS: \"5\"
      WENJIN_AUDIT_SINK: stdout
      WENJIN_AUDIT_INCLUDE_QUERY: \"0\"
    secrets: [model_api_token, file_admin_token]
    volumes:
      - application-data:/app/data
      - application-raw:/app/raw_data
      - application-corpora:/app/corpora
      - application-config:/app/configs
      - delivery-snapshots:/app/qdrant-snapshot
      - delivery-snapshots:/delivery-snapshots
    ports:
      - 127.0.0.1:${{DELIVERY_MANAGEMENT_PORT}}:8777
    labels:
      com.wenjin.delivery.run_id: \"{run_id}\"
      com.wenjin.delivery.source_sha256: \"{source_sha256}\"
    networks: [isolated]
  legacy-mcp:
    image: {image_tag}
    command: [python, -m, wenjin.mcp_server, --host, 0.0.0.0, --port, \"18765\", --path, /mcp, --identity-db, /app/data/file-management/manual-agent-identity.sqlite]
    environment:
      <<: *app-env
      WENJIN_MCP_ALLOW_PRIVATE_HTTP: \"1\"
      WENJIN_MCP_ALLOWED_HOSTS: \"legacy-mcp:18765\"
    secrets: [model_api_token]
    volumes:
      - application-data:/app/data
      - application-raw:/app/raw_data
      - application-corpora:/app/corpora
      - application-config:/app/configs
    ports:
      - 127.0.0.1:${{DELIVERY_MCP_PORT}}:18765
    labels:
      com.wenjin.delivery.run_id: \"{run_id}\"
      com.wenjin.delivery.source_sha256: \"{source_sha256}\"
    networks: [isolated]
  qdrant-restored:
    image: qdrant/qdrant:v1.19.1
    profiles: [restore]
    labels:
      com.wenjin.delivery.run_id: "{run_id}"
      com.wenjin.delivery.source_sha256: "{source_sha256}"
    volumes:
      - qdrant-restored:/qdrant/storage
    expose: ["6333"]
    networks: [isolated]
  management-restored:
    image: {image_tag}
    profiles: [restore]
    command: [python, -m, wenjin.file_management, serve, --root, /delivery-snapshots/restored-project, --host, 0.0.0.0, --port, "8778", --identity-db, /delivery-snapshots/restored-project/data/file-management/manual-agent-identity.sqlite]
    environment:
      <<: *app-env
      QDRANT_URL: http://qdrant-restored:6333
      WENJIN_PROJECT_ROOT: /delivery-snapshots/restored-project
      WENJIN_IDENTITY_DB: /delivery-snapshots/restored-project/data/file-management/manual-agent-identity.sqlite
    secrets: [model_api_token, file_admin_token]
    volumes:
      - delivery-snapshots:/delivery-snapshots
    ports:
      - 127.0.0.1:${{DELIVERY_RESTORED_MANAGEMENT_PORT}}:8778
    labels:
      com.wenjin.delivery.run_id: "{run_id}"
      com.wenjin.delivery.source_sha256: "{source_sha256}"
    networks: [isolated]
  mcp-restored:
    image: {image_tag}
    profiles: [restore]
    command: [python, -m, wenjin.mcp_server, --host, 0.0.0.0, --port, "18766", --path, /mcp, --project-root, /delivery-snapshots/restored-project, --identity-db, /delivery-snapshots/restored-project/data/file-management/manual-agent-identity.sqlite]
    environment:
      <<: *app-env
      QDRANT_URL: http://qdrant-restored:6333
      WENJIN_PROJECT_ROOT: /delivery-snapshots/restored-project
      WENJIN_IDENTITY_DB: /delivery-snapshots/restored-project/data/file-management/manual-agent-identity.sqlite
      WENJIN_MCP_ALLOW_PRIVATE_HTTP: "1"
      WENJIN_MCP_ALLOWED_HOSTS: "mcp-restored:18766"
    secrets: [model_api_token]
    volumes:
      - delivery-snapshots:/delivery-snapshots
    labels:
      com.wenjin.delivery.run_id: "{run_id}"
      com.wenjin.delivery.source_sha256: "{source_sha256}"
    networks: [isolated]
networks:
  isolated:
    internal: true
volumes:
  qdrant-origin:
  qdrant-restored:
  application-data:
  application-raw:
  application-corpora:
  application-config:
  delivery-snapshots:
secrets:
  model_api_token:
    file: secrets/model_api_token
  file_admin_token:
    file: secrets/file_admin_token
"""
        (output / "compose.synthetic.yaml").write_text(compose, encoding="utf-8")
        (output / ".env").write_text(
            f"DELIVERY_IMAGE={image_tag}\n"
            f"DELIVERY_MANAGEMENT_PORT={_free_port()}\n"
            f"DELIVERY_MCP_PORT={_free_port()}\n"
            f"DELIVERY_RESTORED_MANAGEMENT_PORT={_free_port()}\n",
            encoding="utf-8",
        )
        secret_dir = output / "secrets"
        secret_dir.mkdir()
        for name in ("model_api_token", "file_admin_token"):
            path = secret_dir / name
            path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "base_image": BASE_IMAGE,
            "expected_base_image_id": BASE_IMAGE_ID,
            "candidate_image_tag": image_tag,
            "source_sha256": source_sha256,
            "source_files": rows,
            "network": "isolated internal-only compose network",
            "model_api": "synthetic deterministic stub; not a real model validation",
            "docker_execution": "not run by this prepare command",
        }
        (output / "fixture-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if build:
            if not expected_source_sha256:
                raise ValueError("--build requires --expect-source-sha256 as a freeze gate")
            base = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", BASE_IMAGE],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            if base != BASE_IMAGE_ID:
                raise ValueError("cached Docker backend base image ID differs from approved inventory")
            subprocess.run(
                ["docker", "build", "--network=none", "--file", "Dockerfile.delivery",
                 "--tag", image_tag, "."], cwd=output, check=True,
            )
        return manifest
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect-source-sha256")
    parser.add_argument("--build", action="store_true", help="核对缓存base image ID并用--network none构建临时应用镜像")
    args = parser.parse_args(argv)
    try:
        result = prepare(
            args.source_root,
            args.output,
            expected_source_sha256=args.expect_source_sha256,
            build=args.build,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"delivery fixture preparation failed: {type(error).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
