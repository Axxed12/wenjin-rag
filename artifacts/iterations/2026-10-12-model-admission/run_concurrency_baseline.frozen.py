"""Prepare and run a resource-gated, isolated Docker MCP HTTP concurrency baseline.

The `prepare` subcommand only writes an isolated compose project and synthetic
fixture inputs. It never starts containers. `run` is an explicit live operation;
it uses a fresh Docker project, fresh named volumes, two temporary model weights
mounts (read-only), and a temporary Qdrant instance. Credentials stay in memory
or temporary secret files and are removed during cleanup.

No benchmark result is meaningful until `run` completes with its generated
summary.json and cleanup record. Sixteen samples per measured level are small-
sample exploratory percentiles, not an SLO or stable tail-latency claim.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

REPO = Path(__file__).resolve().parents[3]
ARTIFACT_ROOT = Path(__file__).resolve().parent
if str(ARTIFACT_ROOT) not in sys.path:
    sys.path.insert(0, str(ARTIFACT_ROOT))
BACKEND_IMAGE = "wenjin-backend:dual-smoke-20261008"
MODEL_IMAGE = "wenjin-model-node:dual-smoke-20261008"
EMBEDDING_ID = "Qwen3-Embedding-0.6B"
RERANKER_ID = "Qwen3-Reranker-0.6B"
PROTOCOL = "2025-11-25"
CONCURRENCY_LEVELS = (1, 2, 4, 8)
REQUESTS_PER_LEVEL = 16
HTTP_TIMEOUT_SECONDS = 60
RAM_STOP_MIB = 2048
GPU_FREE_STOP_MIB = 512
GPU_START_FREE_MIB = 8192
RAM_START_FREE_MIB = 6144
FAILURE_STOP_RATIO = 0.05
SAMPLE_INTERVAL_SECONDS = 1.0
TERMINAL_JOBS = {"completed", "failed", "deleted", "cancelled", "duplicate"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_tree_hash() -> str:
    digest = hashlib.sha256()
    source_root = REPO / "src"
    for path in sorted(source_root.rglob("*.py")):
        digest.update(path.relative_to(source_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def run_command(args: list[str], *, cwd: Path = REPO, timeout: int = 120) -> str:
    completed = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace", check=False,
    )
    if completed.returncode:
        # Do not relay arbitrary command output: it may include environment values.
        raise RuntimeError(f"command_failed:{Path(args[0]).name}:{completed.returncode}")
    return completed.stdout.strip()


def free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def host_memory_mib() -> tuple[int | None, int | None]:
    if os.name == "nt":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys // (1024 * 1024), status.ullAvailPhys // (1024 * 1024)
        return None, None
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        available = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return pages * page_size // (1024 * 1024), available * page_size // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        return None, None


def gpu_stats() -> dict[str, int] | None:
    try:
        output = run_command([
            "nvidia-smi", "--query-gpu=index,memory.total,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits",
        ], timeout=5)
        first = output.splitlines()[0].split(",")
        values = [int(item.strip()) for item in first]
        return {"index": values[0], "total_mib": values[1], "free_mib": values[2],
                "utilization_percent": values[3]}
    except (OSError, IndexError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        return None


def resource_snapshot() -> dict[str, Any]:
    total, free = host_memory_mib()
    return {"time_utc": utc_now(), "monotonic": time.monotonic(),
            "host_memory_total_mib": total, "host_memory_free_mib": free,
            "gpu": gpu_stats()}


def write_new(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")


def create_overlay(path: Path, ports: dict[str, int], runtime_root: Path) -> None:
    # Compose v2 !override replaces only application data mounts; model weights and
    # source code are separate read-only binds. Qdrant is exposed only on loopback
    # so the host can seed a real-weight vector before MCP accepts traffic.
    src = (REPO / "src").as_posix()
    root = runtime_root.as_posix()
    overlay = f'''services:
  qdrant:
    ports: !override
      - "127.0.0.1:{ports['qdrant']}:6333"
    networks: !override
      - data-plane
      - client-plane
  management:
    image: {BACKEND_IMAGE}
    volumes: !override
      - type: bind
        source: {root}/data
        target: /app/data
      - type: bind
        source: {root}/identity
        target: /var/lib/wenjin
      - type: bind
        source: {root}/artifacts
        target: /app/artifacts
      - type: bind
        source: {root}/corpora
        target: /app/corpora
        read_only: true
      - type: bind
        source: {root}/configs
        target: /app/configs
        read_only: true
      - type: bind
        source: {root}/data/indexes
        target: /app/data/indexes
        read_only: true
      - type: bind
        source: {root}/raw_data
        target: /app/raw_data
        read_only: true
      - type: bind
        source: {src}
        target: /app/src
        read_only: true
    environment:
      PYTHONPATH: /app/src
      WENJIN_AUDIT_INCLUDE_QUERY: "0"
      WENJIN_AUDIT_SINK: stdout
    networks: !override
      - data-plane
      - client-plane
  mcp:
    image: {BACKEND_IMAGE}
    volumes: !override
      - type: bind
        source: {root}/data
        target: /app/data
        read_only: true
      - type: bind
        source: {root}/identity
        target: /var/lib/wenjin
      - type: bind
        source: {root}/artifacts
        target: /app/artifacts
        read_only: true
      - type: bind
        source: {root}/corpora
        target: /app/corpora
        read_only: true
      - type: bind
        source: {root}/configs
        target: /app/configs
        read_only: true
      - type: bind
        source: {root}/data/indexes
        target: /app/data/indexes
        read_only: true
      - type: bind
        source: {root}/raw_data
        target: /app/raw_data
        read_only: true
      - type: bind
        source: {src}
        target: /app/src
        read_only: true
    environment:
      PYTHONPATH: /app/src
      WENJIN_AUDIT_INCLUDE_QUERY: "0"
      WENJIN_AUDIT_SINK: stdout
    networks: !override
      - data-plane
      - client-plane
  model-node:
    image: {MODEL_IMAGE}
    ports: !override
      - "127.0.0.1:{ports['model']}:8765"
    volumes: !override
      - type: bind
        source: ${{EMBEDDING_MODEL_PATH:?Set embedding weight path}}
        target: /models/embedding
        read_only: true
      - type: bind
        source: ${{RERANKER_MODEL_PATH:?Set reranker weight path}}
        target: /models/reranker
        read_only: true
      - type: bind
        source: {src}
        target: /app/src
        read_only: true
    environment:
      PYTHONPATH: /app/src
    networks: !override
      - data-plane
      - client-plane
networks:
  client-plane: {{}}
'''
    # Keep exactly one mapping per Compose service. Compose's YAML loader may
    # silently replace duplicate service keys, which can discard mounts.
    service_keys = re.findall(r"^  ([A-Za-z0-9_-]+):\s*$", overlay, flags=re.MULTILINE)
    if len(service_keys) != len(set(service_keys)):
        raise RuntimeError("compose_overlay_duplicate_service_key")
    write_new(path, overlay.encode("utf-8"), mode=0o600)


def compose_args(state: dict[str, Any]) -> list[str]:
    return [
        "docker", "compose", "--project-directory", str(REPO),
        "--env-file", str(Path(state["env_file"])), "-p", state["project"],
        "-f", str(REPO / "deploy/compose.base.yaml"),
        "-f", str(REPO / "deploy/compose.local.yaml"),
        "-f", str(state["overlay_file"]),
    ]


def docker_image_id(image: str) -> str:
    return run_command(["docker", "image", "inspect", image, "--format", "{{.Id}}"], timeout=15)


def attest_loaded_source(compose: list[str], service: str, expected_sha256: str) -> dict[str, str]:
    code = (
        "import hashlib,json,pathlib,wenjin; "
        "p=pathlib.Path(wenjin.__file__).resolve(); r=p.parent.parent; h=hashlib.sha256(); "
        "[(h.update(x.relative_to(r).as_posix().encode()),h.update(b'\\0'),h.update(x.read_bytes()),h.update(b'\\0')) "
        "for x in sorted(r.rglob('*.py'))]; "
        "print(json.dumps({'wenjin_file':str(p),'source_root':str(r),'sha256':h.hexdigest()}))"
    )
    raw = run_command(compose + ["exec", "-T", service, "python", "-c", code], timeout=30)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"source_attestation_invalid:{service}") from exc
    if result.get("sha256") != expected_sha256 or result.get("source_root") != "/app/src":
        raise RuntimeError(f"source_attestation_mismatch:{service}")
    return {"service": service, "wenjin_file": result["wenjin_file"],
            "source_root": result["source_root"], "sha256": result["sha256"],
            "matches_host_source": True}


def docker_ready() -> None:
    run_command(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    docker_ready()
    for image in (BACKEND_IMAGE, MODEL_IMAGE):
        docker_image_id(image)
    embedding_path = Path(args.embedding_path).resolve()
    reranker_path = Path(args.reranker_path).resolve()
    if not embedding_path.is_dir() or not reranker_path.is_dir():
        raise RuntimeError("model_weight_directory_missing")
    resources = resource_snapshot()
    if (resources["host_memory_free_mib"] is None
            or resources["host_memory_free_mib"] < RAM_START_FREE_MIB
            or resources["gpu"] is None
            or resources["gpu"]["free_mib"] < GPU_START_FREE_MIB):
        raise RuntimeError("resource_start_gate_rejected")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    project = "wenjin-concurrency-" + uuid.uuid4().hex[:8]
    evidence_dir = ARTIFACT_ROOT / "runs" / run_id
    if evidence_dir.exists():
        raise FileExistsError("evidence_run_directory_exists")
    evidence_dir.mkdir(parents=True)
    temp_parent = Path(tempfile.gettempdir()) / "wenjin-concurrency-baseline"
    temp_parent.mkdir(parents=True, exist_ok=True)
    runtime_root = temp_parent / project
    runtime_root.mkdir(exist_ok=False)
    for name in ("data/indexes", "data", "identity", "artifacts", "corpora", "configs", "raw_data", "secrets"):
        (runtime_root / name).mkdir(parents=True, exist_ok=True)
    ports = {"mcp": free_loopback_port(), "admin": free_loopback_port(),
             "qdrant": free_loopback_port(), "model": free_loopback_port()}
    secrets_map = {"model_api_token": uuid.uuid4().hex + uuid.uuid4().hex,
                   "file_admin_token": uuid.uuid4().hex + uuid.uuid4().hex}
    secret_paths = {key: runtime_root / "secrets" / key for key in secrets_map}
    for key, value in secrets_map.items():
        write_new(secret_paths[key], (value + "\n").encode("ascii"))
    env_file = runtime_root / ".env.concurrency"
    env = {
        "WENJIN_INDEX_ROOT": str(runtime_root / "data" / "indexes").replace("\\", "/"),
        "WENJIN_RAW_DATA_ROOT": str(runtime_root / "raw_data").replace("\\", "/"),
        "WENJIN_CORPORA_ROOT": str(runtime_root / "corpora").replace("\\", "/"),
        "WENJIN_CONFIG_ROOT": str(runtime_root / "configs").replace("\\", "/"),
        "EMBEDDING_MODEL_PATH": embedding_path.as_posix(),
        "RERANKER_MODEL_PATH": reranker_path.as_posix(),
        "EMBEDDING_MODEL_ID": EMBEDDING_ID,
        "RERANKER_MODEL_ID": RERANKER_ID,
        "WENJIN_MODEL_API_TOKEN_FILE": secret_paths["model_api_token"].as_posix(),
        "WENJIN_FILE_ADMIN_TOKEN_FILE": secret_paths["file_admin_token"].as_posix(),
        "WENJIN_MODEL_API_URL": "http://model-node:8765",
        "WENJIN_ADMIN_BIND_ADDRESS": "127.0.0.1",
        "WENJIN_ADMIN_PORT": str(ports["admin"]),
        "MCP_BIND_ADDRESS": "127.0.0.1",
        "MCP_PORT": str(ports["mcp"]),
        "WENJIN_MCP_ALLOWED_HOSTS": f"localhost:{ports['mcp']},127.0.0.1:{ports['mcp']}",
        "WENJIN_FILE_MAX_UPLOAD_BYTES": "1048576",
        "SMOKE_ROOT": runtime_root.as_posix(),
    }
    env_body = "".join(f'{key}="{value}"\n' for key, value in env.items())
    write_new(env_file, env_body.encode("utf-8"))
    overlay_file = ARTIFACT_ROOT / f"compose.concurrency.{run_id}.yaml"
    create_overlay(overlay_file, ports, runtime_root)
    state = {
        "schema_version": 1, "run_id": run_id, "project": project,
        "runtime_root": str(runtime_root), "evidence_dir": str(evidence_dir),
        "env_file": str(env_file), "overlay_file": str(overlay_file),
        "ports": ports, "embedding_path": str(embedding_path),
        "reranker_path": str(reranker_path), "model_secret_path": str(secret_paths["model_api_token"]),
        "admin_secret_path": str(secret_paths["file_admin_token"]),
        "prepared_utc": utc_now(), "source_sha256": source_tree_hash(),
        "host_resource_snapshot": resources,
    }
    state_file = ARTIFACT_ROOT / f"run-state.{run_id}.json"
    state["state_file"] = str(state_file)
    write_new(state_file, (json.dumps(state, indent=2) + "\n").encode("utf-8"), mode=0o600)
    compose = compose_args(state)
    run_command(compose + ["config", "--quiet"], timeout=60)
    metadata = {
        "run_id": run_id, "project": project, "state_file": state_file.name,
        "evidence_dir": evidence_dir.relative_to(ARTIFACT_ROOT).as_posix(),
        "ports": ports, "image_ids": {BACKEND_IMAGE: docker_image_id(BACKEND_IMAGE),
                                       MODEL_IMAGE: docker_image_id(MODEL_IMAGE)},
        "source_sha256": state["source_sha256"],
        "weights": {"embedding_mount": "read-only", "reranker_mount": "read-only",
                    "asset_hashes_recorded_from_model_identity_after_start": True},
        "runtime_root_is_temporary": True,
        "credentials": "generated ephemerally; values not recorded",
        "compose_config": "validated; services not started",
        "resource_gates": {"start_free_ram_mib": RAM_START_FREE_MIB,
                           "start_free_gpu_mib": GPU_START_FREE_MIB,
                           "stop_free_ram_mib": RAM_STOP_MIB,
                           "stop_free_gpu_mib": GPU_FREE_STOP_MIB,
                           "failure_ratio": FAILURE_STOP_RATIO},
    }
    write_new(evidence_dir / "prepared.json", (json.dumps(metadata, indent=2) + "\n").encode())
    # Only non-secret launch metadata is printed.
    print(json.dumps({"prepared": True, "run_id": run_id, "project": project,
                      "state_file": str(state_file), "evidence_dir": str(evidence_dir),
                      "compose_config": "valid", "services_started": False,
                      "credentials": "not printed"}, indent=2))
    return state


def directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    files = [path for path in root.rglob("*") if path.is_file()]
    for path in sorted(files):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def bootstrap_isolated_identity_db(db_path: Path, password: str) -> None:
    """Bootstrap only the disposable DB before Docker's bridge IP guard applies."""
    source = str(REPO / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from wenjin.identity.store import IdentityStore

    store = IdentityStore(db_path.resolve())
    store.initialize()
    store.bootstrap_first_admin("bench-admin", password)


class Sampler:
    def __init__(self, state: dict[str, Any], output: Path):
        self.state = state
        self.output = output
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.samples: list[dict[str, Any]] = []
        self.stop_reason: str | None = None

    def start(self) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text("", encoding="utf-8")
        self.thread = threading.Thread(target=self._sample_loop, name="resource-sampler", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _sample_loop(self) -> None:
        while not self.stop_event.is_set():
            item = resource_snapshot()
            try:
                listing = run_command(["docker", "ps", "-a", "--no-trunc", "--filter",
                                       f"label=com.docker.compose.project={self.state['project']}",
                                       "--format", '{{.ID}}|{{.Names}}|{{.Label "com.docker.compose.service"}}|{{.State}}'],
                                      timeout=5)
                containers = [line.split("|", 3) for line in listing.splitlines() if line.strip()]
                running_ids = [container[0] for container in containers if container[3].lower() == "running"]
                raw_stats = (run_command(["docker", "stats", "--no-stream", "--format",
                                          "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}",
                                          *running_ids], timeout=5)
                             if running_ids else "")
                stats_by_name: dict[str, list[str]] = {}
                for line in raw_stats.splitlines():
                    fields = line.split("|", 3)
                    if len(fields) == 4:
                        stats_by_name[fields[0]] = fields[1:]
                lifecycle_by_id: dict[str, list[str]] = {}
                if containers:
                    inspect = run_command(["docker", "inspect", "--format",
                                           "{{.Id}}|{{.State.OOMKilled}}|{{.State.ExitCode}}|{{.State.Status}}",
                                           *[container[0] for container in containers]], timeout=5)
                    for line in inspect.splitlines():
                        fields = line.split("|", 3)
                        if len(fields) == 4:
                            lifecycle_by_id[fields[0][:12]] = fields[1:]
                item["containers"] = []
                for container_id, name, service, state_text in containers:
                    lifecycle = lifecycle_by_id.get(container_id[:12], [])
                    item["containers"].append({"service": service, "container_id": container_id[:12],
                                                "state": state_text, "stats": stats_by_name.get(name),
                                                "lifecycle": lifecycle})
                    if lifecycle and lifecycle[0].lower() == "true":
                        self.stop_reason = "container_oom_killed"
                if any("exited" in str(container.get("lifecycle", [])).lower()
                       for container in item["containers"]):
                    self.stop_reason = self.stop_reason or "container_exited"
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                item["container_sample_error"] = "docker_stats_unavailable"
            if item["host_memory_free_mib"] is not None and item["host_memory_free_mib"] < RAM_STOP_MIB:
                self.stop_reason = "host_free_memory_below_2048_mib"
            if item["gpu"] is not None and item["gpu"]["free_mib"] < GPU_FREE_STOP_MIB:
                self.stop_reason = "gpu_free_memory_below_512_mib"
            self.samples.append(item)
            append_jsonl(self.output, item)
            if self.stop_reason:
                self.stop_event.set()
                continue
            self.stop_event.wait(SAMPLE_INTERVAL_SECONDS)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def read_state(path: Path) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    expected_root = (Path(tempfile.gettempdir()) / "wenjin-concurrency-baseline").resolve()
    root = Path(state["runtime_root"]).resolve()
    if not root.is_relative_to(expected_root) or not state["project"].startswith("wenjin-concurrency-"):
        raise RuntimeError("state_is_not_within_isolated_temp_root")
    if not Path(state["state_file"]).resolve().is_relative_to(ARTIFACT_ROOT.resolve()):
        raise RuntimeError("state_file_outside_iteration_root")
    return state


def wait_http(url: str, *, headers: dict[str, str] | None = None,
              expect_json: bool = False, timeout_seconds: int = 900,
              stop_event: threading.Event | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_kind = "not_ready"
    with httpx.Client(timeout=3, trust_env=False) as client:
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("resource_safety_gate_stopped_during_readiness")
            try:
                response = client.get(url, headers=headers)
                if response.status_code == 200:
                    return response.json() if expect_json else {"status": response.status_code}
                last_kind = f"http_{response.status_code}"
            except httpx.HTTPError as exc:
                last_kind = type(exc).__name__
            time.sleep(1)
    raise TimeoutError(f"service_readiness_timeout:{last_kind}")


def remote_embedder(state: dict[str, Any], token: str):
    from wenjin.models.providers import RemoteEmbedding, RemoteModelClient

    client = RemoteModelClient(
        f"http://127.0.0.1:{state['ports']['model']}", token,
        expected_embedding_model=EMBEDDING_ID, expected_reranker_model=RERANKER_ID,
        timeout_seconds=HTTP_TIMEOUT_SECONDS,
    )
    return client, RemoteEmbedding(client)


def seed_shared_fixture(state: dict[str, Any], token: str) -> dict[str, Any]:
    """Create one real-weight shared vector corpus in the run-owned Qdrant volume."""
    from qdrant_client import QdrantClient

    from wenjin.content_hashing import (
        TEXT_NEWLINE_LF_V1,
        hash_content,
        manifest_content_fingerprint,
    )
    from wenjin.index_registry import IndexRegistryStore
    from wenjin.indexing.build import build_candidate_index

    root = Path(state["runtime_root"])
    library = "synthetic-concurrency"
    version = "1.0"
    corpus_id = f"{library}-{version}"
    marker = "synthetic_concurrency_evidence_only"
    content = ("Synthetic concurrency baseline corpus.\n"
               f"Unique evidence marker: {marker}.\n"
               "This content exists only in the isolated benchmark fixture.\n").encode("utf-8")
    raw_rel = Path(library) / version / "guide.txt"
    raw_path = root / "raw_data" / raw_rel
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(content)
    hashed = hash_content(content, normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8")
    manifest = {
        "schema_version": 3, "corpus_id": corpus_id, "library": library,
        "version": version, "license": "synthetic-test-only",
        "source_url": f"urn:synthetic:{library}:{version}",
        "raw_data_subdir": str(Path(library) / version).replace("\\", "/"),
        "files": [{"path": "guide.txt", "raw_sha256": hashed.raw_sha256,
                   "canonical_sha256": hashed.canonical_sha256,
                   "normalization": TEXT_NEWLINE_LF_V1, "encoding": "utf-8",
                   "bytes": len(content), "indexable": True, "parser": "text-line-v1",
                   "source_url": f"urn:synthetic:{library}:{version}/guide.txt",
                   "logical_document_id": f"{corpus_id}:guide.txt"}],
    }
    manifest["canonical_corpus_sha256"] = manifest_content_fingerprint(manifest)
    manifest_path = root / "corpora" / corpus_id / "source-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    qdrant = QdrantClient(url=f"http://127.0.0.1:{state['ports']['qdrant']}", timeout=10)
    model_client = None
    try:
        model_client, embedder = remote_embedder(state, token)
        target = build_candidate_index(
            manifest_path=manifest_path, target_id=f"{corpus_id}-target",
            strategy="blocks-split", dense_collection="concurrency_shared_v1",
            bm25_path=root / "data" / "indexes" / "concurrency_shared.sqlite",
            qdrant_client=qdrant, embedder=embedder, embedding_model=EMBEDDING_ID,
            project_root=root,
        )
        registry = IndexRegistryStore(root / "data" / "indexes" / "index-registry.json")
        registry.initialize(target, reason="temporary concurrency baseline fixture")
        return {
            "library": library, "version": version, "corpus_id": corpus_id,
            "target_id": target.target_id, "collection": target.dense_collection,
            "marker": marker, "raw_sha256": hashed.raw_sha256,
            "manifest_sha256": sha256_file(manifest_path),
            "registry_sha256": sha256_file(root / "data" / "indexes" / "index-registry.json"),
            "vector_dimension": target.vector_dimension,
            "fixture": "synthetic; real remote embedding; Qdrant belongs to current temporary compose project",
        }
    finally:
        if model_client is not None:
            model_client.close()
        qdrant.close()


def extract_sse_json(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" not in content_type:
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("mcp_response_invalid")
        return value
    for line in response.text.splitlines():
        if line.startswith("data:"):
            value = json.loads(line[5:].strip())
            if isinstance(value, dict) and "jsonrpc" in value:
                return value
    raise ValueError("mcp_sse_message_missing")


def structured_tool_result(rpc: dict[str, Any]) -> dict[str, Any]:
    if "error" in rpc:
        return {"_rpc_error": rpc["error"]}
    result = rpc.get("result")
    if not isinstance(result, dict):
        return {"_invalid_result": True}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured | {"_is_error": bool(result.get("isError"))}
    for item in result.get("content", []):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            try:
                parsed = json.loads(item["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed | {"_is_error": bool(result.get("isError"))}
    return {"_is_error": bool(result.get("isError")), "_invalid_result": True}


def safe_http_error_category(status_code: int, body: str) -> str:
    """Classify only explicit model-busy 503s; never persist response bodies."""
    if status_code != 503:
        return "http_error"
    try:
        payload = json.loads(body[:2048])
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and payload.get("error") == "inference_busy":
        return "model_busy"
    return "unknown_503"


def redact_protocol_value(value: Any, token: str) -> Any:
    if isinstance(value, dict):
        return {str(key): ("[REDACTED]" if any(part in str(key).lower()
                for part in ("token", "password", "secret", "authorization", "cookie"))
                else redact_protocol_value(item, token)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_protocol_value(item, token) for item in value]
    if isinstance(value, str):
        value = value.replace(token, "[REDACTED]") if token else value
        value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
        return value[:16000]
    return value


class McpHttpSession:
    """One account token, one real 2025-11-25 HTTP session, used by one worker."""
    def __init__(self, client: httpx.AsyncClient, session_id: str, session_hash: str,
                 protocol: str, capabilities: dict[str, Any]):
        self.client, self.session_id, self.session_hash = client, session_id, session_hash
        self.protocol, self.capabilities = protocol, capabilities
        self.sequence = 1
        self.closed = False

    @classmethod
    async def open(cls, base_url: str, token: str, label: str, ordinal: int) -> "McpHttpSession":
        client = httpx.AsyncClient(base_url=base_url, timeout=HTTP_TIMEOUT_SECONDS, trust_env=False)
        auth_headers = {"Authorization": f"Bearer {token}",
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json"}
        payload = {"jsonrpc": "2.0", "id": f"{label}-init-{ordinal}", "method": "initialize",
                   "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                              "clientInfo": {"name": "concurrency-baseline", "version": "1"}}}
        response = await client.post("/mcp", headers=auth_headers, json=payload)
        if response.status_code != 200:
            await client.aclose()
            raise RuntimeError(f"mcp_initialize_http_{response.status_code}")
        rpc = extract_sse_json(response)
        init = rpc.get("result", {})
        session_id = response.headers.get("Mcp-Session-Id") or response.headers.get("MCP-Session-Id")
        if not session_id or not isinstance(init, dict):
            await client.aclose()
            raise RuntimeError("mcp_stateful_session_missing")
        session_hash = hashlib.sha256(session_id.encode()).hexdigest()
        obj = cls(client, session_id, session_hash, init.get("protocolVersion", ""),
                  init.get("capabilities", {}))
        obj.bind_token(token)
        try:
            accepted = init.get("protocolVersion")
            if accepted != PROTOCOL:
                raise RuntimeError("mcp_protocol_version_mismatch")
            session_headers = auth_headers | {"MCP-Session-Id": session_id}
            initialized = await client.post("/mcp", headers=session_headers,
                                            json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            if initialized.status_code not in {200, 202}:
                raise RuntimeError(f"mcp_initialized_http_{initialized.status_code}")
            check = await obj.call("tools/list", {})
            if "_rpc_error" in check.get("tool", {}) or not isinstance(check.get("tool", {}).get("tools"), list):
                raise RuntimeError("mcp_tools_list_failed")
            return obj
        except Exception:
            await obj.close()
            raise

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.sequence
        self.sequence += 1
        headers = {"Authorization": "Bearer "+self._token,  # set by bind_token()
                   "MCP-Session-Id": self.session_id,
                   "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        response = await self.client.post("/mcp", headers=headers,
                                          json={"jsonrpc": "2.0", "id": request_id,
                                                "method": method, "params": params})
        rpc: dict[str, Any] = {}
        parse_error = None
        if response.status_code == 200:
            try:
                rpc = extract_sse_json(response)
            except (ValueError, json.JSONDecodeError) as exc:
                parse_error = type(exc).__name__
        if method == "tools/list" and isinstance(rpc.get("result"), dict):
            tool = rpc["result"]
        else:
            tool = structured_tool_result(rpc) if response.status_code == 200 else {}
        raw_text = response.text[:16000] if parse_error or response.status_code != 200 else None
        if raw_text:
            raw_text = raw_text.replace(self._token, "[REDACTED]")
            raw_text = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", raw_text)
            raw_text = re.sub(r"(?i)(password|secret|token)(\s*[:=]\s*)[^\s,\"']+",
                              r"\1\2[REDACTED]", raw_text)
        return {"http_status": response.status_code, "http_error_category":
                safe_http_error_category(response.status_code, response.text),
                "rpc": redact_protocol_value(rpc, self._token),
                "tool": redact_protocol_value(tool, self._token), "request_id": request_id,
                "transport_parse_error": parse_error,
                "raw_response_text": raw_text,
                "content_type": response.headers.get("content-type")}

    def bind_token(self, token: str) -> None:
        self._token = token

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.client.delete("/mcp", headers={
                "Authorization": "Bearer "+self._token, "MCP-Session-Id": self.session_id,
                "Accept": "application/json, text/event-stream",
            }, timeout=3)
        except (httpx.HTTPError, RuntimeError):
            pass
        await self.client.aclose()


class Benchmark:
    def __init__(self, state: dict[str, Any], output: Path, sampler: Sampler,
                 stop_event: threading.Event):
        self.state, self.output, self.sampler = state, output, sampler
        self.stop_event = stop_event
        self.records: list[dict[str, Any]] = []
        self.sessions: list[McpHttpSession] = []
        self.phase_summaries: list[dict[str, Any]] = []

    def admin_login(self, base_url: str, password: str) -> tuple[httpx.Client, dict[str, Any]]:
        admin = httpx.Client(base_url=base_url, timeout=HTTP_TIMEOUT_SECONDS, trust_env=False,
                             headers={"Origin": base_url})
        login = admin.post("/api/auth/login", json={"username": "bench-admin", "password": password})
        if login.status_code != 200:
            raise RuntimeError(f"admin_login_http_{login.status_code}")
        csrf = login.json().get("csrf_token", "")
        if not csrf:
            raise RuntimeError("admin_csrf_missing")
        return admin, {"X-CSRF-Token": csrf, "Origin": base_url}

    def create_managed_reader(self, admin: httpx.Client, admin_headers: dict[str, str]) -> dict[str, Any]:
        display_name = "Synthetic concurrency managed fixture"
        created = admin.post("/api/admin/managed-libraries", headers=admin_headers,
                             json={"display_name": display_name})
        if created.status_code != 201:
            raise RuntimeError(f"managed_library_create_http_{created.status_code}")
        library_id = created.json()["library"]["library_id"]
        reader_password = "B8!" + uuid.uuid4().hex + "Y"
        reader = admin.post("/api/admin/users", headers=admin_headers,
                            json={"username": "bench-reader", "password": reader_password,
                                  "role": "reader"})
        if reader.status_code != 201:
            raise RuntimeError(f"reader_create_http_{reader.status_code}")
        user_id = reader.json()["user"]["user_id"]
        reader_http = httpx.Client(base_url=str(admin.base_url).rstrip("/"), timeout=HTTP_TIMEOUT_SECONDS,
                                   trust_env=False, headers={"Origin": str(admin.base_url).rstrip("/")})
        first = reader_http.post("/api/auth/login", json={"username": "bench-reader", "password": reader_password})
        if first.status_code != 200:
            raise RuntimeError(f"reader_first_login_http_{first.status_code}")
        csrf = first.json().get("csrf_token", "")
        change = reader_http.post("/api/auth/password", headers={"X-CSRF-Token": csrf},
                                  json={"old_password": reader_password,
                                        "new_password": reader_password + "-changed"})
        if change.status_code != 200:
            raise RuntimeError(f"reader_password_change_http_{change.status_code}")
        login = reader_http.post("/api/auth/login", json={"username": "bench-reader",
                                                           "password": reader_password + "-changed"})
        if login.status_code != 200:
            raise RuntimeError(f"reader_login_http_{login.status_code}")
        reader_csrf = login.json().get("csrf_token", "")
        credential = reader_http.post("/api/auth/credentials", headers={"X-CSRF-Token": reader_csrf},
                                      json={"label": "isolated-concurrency-reader"})
        if credential.status_code != 201:
            raise RuntimeError(f"reader_credential_http_{credential.status_code}")
        token = credential.json()["credential"]["token"]
        reader_http.close()
        return {"library_id": library_id, "user_id": user_id, "mcp_token": token,
                "reader_password": reader_password + "-changed", "display_name": display_name}

    def create_unprivileged_reader(self, admin: httpx.Client,
                                   admin_headers: dict[str, str]) -> dict[str, Any]:
        """Create a valid second MCP identity that receives no grants."""
        password = "C7!" + uuid.uuid4().hex + "W"
        created = admin.post("/api/admin/users", headers=admin_headers,
                             json={"username": "bench-reader-b", "password": password,
                                   "role": "reader"})
        if created.status_code != 201:
            raise RuntimeError(f"unprivileged_reader_create_http_{created.status_code}")
        user_id = created.json()["user"]["user_id"]
        client = httpx.Client(base_url=str(admin.base_url).rstrip("/"), timeout=HTTP_TIMEOUT_SECONDS,
                              trust_env=False, headers={"Origin": str(admin.base_url).rstrip("/")})
        first = client.post("/api/auth/login", json={"username": "bench-reader-b", "password": password})
        if first.status_code != 200:
            raise RuntimeError(f"unprivileged_reader_first_login_http_{first.status_code}")
        csrf = first.json().get("csrf_token", "")
        changed_password = password + "-changed"
        changed = client.post("/api/auth/password", headers={"X-CSRF-Token": csrf},
                              json={"old_password": password, "new_password": changed_password})
        if changed.status_code != 200:
            raise RuntimeError(f"unprivileged_reader_password_change_http_{changed.status_code}")
        login = client.post("/api/auth/login", json={"username": "bench-reader-b", "password": changed_password})
        if login.status_code != 200:
            raise RuntimeError(f"unprivileged_reader_login_http_{login.status_code}")
        issued = client.post("/api/auth/credentials",
                             headers={"X-CSRF-Token": login.json().get("csrf_token", "")},
                             json={"label": "isolated-concurrency-unprivileged"})
        if issued.status_code != 201:
            raise RuntimeError(f"unprivileged_reader_credential_http_{issued.status_code}")
        token = issued.json()["credential"]["token"]
        client.close()
        return {"user_id": user_id, "mcp_token": token,
                "reader_password": changed_password}

    def upload_managed(self, admin: httpx.Client, admin_headers: dict[str, str],
                       reader: dict[str, Any], version_marker: str) -> dict[str, Any]:
        content = (f"Synthetic managed concurrency source.\nUnique marker: {version_marker}.\n"
                   "Temporary benchmark data only.\n").encode("utf-8")
        upload = admin.post(f"/api/files?library_id={quote(reader['library_id'], safe='')}",
                            headers=admin_headers | {"Content-Type": "application/octet-stream",
                                                     "X-File-Name": "managed-guide.txt"}, content=content)
        if upload.status_code != 202:
            raise RuntimeError(f"managed_upload_http_{upload.status_code}")
        job = self.wait_job(admin, upload.json()["job_id"])
        if job.get("status") != "completed":
            raise RuntimeError("managed_upload_job_failed")
        file_id = job["file_id"]
        info = admin.get(f"/api/files/{quote(file_id, safe='')}")
        if info.status_code != 200:
            raise RuntimeError(f"managed_file_info_http_{info.status_code}")
        file_info = info.json()
        return {"file_id": file_id, "library_id": reader["library_id"],
                "version_id": file_info.get("active_version_id"), "marker": version_marker,
                "content_sha256": hashlib.sha256(content).hexdigest()}

    def grant_resources(self, admin: httpx.Client, admin_headers: dict[str, str],
                        reader: dict[str, Any], file_id: str,
                        shared_library_id: str) -> list[dict[str, Any]]:
        # Synchronize the inventory before granting. This registers only the current
        # synthetic shared target and the test-owned managed library/file.
        resources = admin.get("/api/admin/resources")
        if resources.status_code != 200:
            raise RuntimeError(f"identity_resource_sync_http_{resources.status_code}")
        grants = []
        for kind, resource_id in (("library", shared_library_id),
                                  ("library", reader["library_id"]), ("managed_file", file_id)):
            grant = admin.post("/api/admin/grants", headers=admin_headers,
                               json={"user_id": reader["user_id"], "resource_kind": kind,
                                     "resource_id": resource_id, "allowed": True,
                                     "expected_revision": 0})
            if grant.status_code != 200:
                raise RuntimeError(f"managed_grant_{kind}_http_{grant.status_code}")
            grant_record = grant.json().get("grant", {})
            grants.append({"resource_kind": kind,
                           "resource_id_sha256": hashlib.sha256(resource_id.encode()).hexdigest(),
                           "allowed": grant_record.get("allowed"),
                           "revision": grant_record.get("revision")})
        return grants

    async def verify_default_deny(self, base_url: str, token: str, file_id: str) -> dict[str, Any]:
        # Before the grants exist, a valid account credential must receive no evidence.
        session = await McpHttpSession.open(base_url, token, "denied", 1)
        session.bind_token(token)
        try:
            response = await session.call("tools/call", {"name": "search_docs",
                                                          "arguments": {"file_id": file_id,
                                                                        "query": "synthetic managed marker",
                                                                        "top_k": 1}})
            tool = response["tool"]
            evidence = tool.get("evidence", []) if isinstance(tool, dict) else []
            if evidence:
                raise RuntimeError("unauthorized_managed_search_returned_evidence")
            result = {"http_status": response["http_status"], "evidence_count": 0,
                      "request_id": tool.get("request_id"),
                      "denial_error": tool.get("error"),
                      "session_sha256": session.session_hash,
                      "authorization_path": "personal MCP credential; no library/file grant"}
            write_json(self.output / "controlled-default-deny.json", result)
            return result
        finally:
            await session.close()

    async def verify_unprivileged_identity(self, base_url: str, token: str,
                                           shared_target_id: str, file_id: str,
                                           shared_marker: str, managed_marker: str) -> dict[str, Any]:
        """Interleave shared and managed probes from a valid but ungranted user."""
        session = await McpHttpSession.open(base_url, token, "reader-b", 1)
        session.bind_token(token)
        checks = []
        authorized: McpHttpSession | None = None
        try:
            authorized = await McpHttpSession.open(base_url, self.reader_token, "reader-a", 1)
            authorized.bind_token(self.reader_token)
            for label, args, marker in (
                ("shared", {"query": "synthetic concurrency evidence", "target_id": shared_target_id,
                            "top_k": 1}, shared_marker),
                ("managed", {"query": "synthetic managed marker", "file_id": file_id,
                             "top_k": 1}, managed_marker),
            ):
                allowed, denied = await asyncio.gather(
                    authorized.call("tools/call", {"name": "search_docs", "arguments": args}),
                    session.call("tools/call", {"name": "search_docs", "arguments": args}),
                )
                allowed_tool, denied_tool = allowed["tool"], denied["tool"]
                allowed_evidence = allowed_tool.get("evidence", []) if isinstance(allowed_tool, dict) else []
                denied_evidence = denied_tool.get("evidence", []) if isinstance(denied_tool, dict) else []
                marker_match = any(marker in str(item.get("text", "")) for item in allowed_evidence)
                identity_match = (marker_match and allowed.get("http_status") == 200
                                  and not allowed_tool.get("error"))
                if label == "shared" and allowed_evidence:
                    identity_match = identity_match and all(
                        item.get("library") == "synthetic-concurrency"
                        and item.get("version") == "1.0"
                        and item.get("index_target_id") == shared_target_id
                        for item in allowed_evidence
                    )
                if label == "managed" and allowed_evidence:
                    identity = allowed_tool.get("managed_file", {})
                    identity_match = identity_match and identity.get("file_id") == file_id and all(
                        item.get("file_id") == file_id
                        and item.get("version_id") == identity.get("version_id")
                        for item in allowed_evidence
                    )
                checks.extend([
                    {"case": f"{label}-authorized-A", "http_status": allowed["http_status"],
                     "request_id": allowed_tool.get("request_id"), "evidence_count": len(allowed_evidence),
                     "expected_marker_match": marker_match, "expected_identity_match": identity_match,
                     "session_sha256": authorized.session_hash,
                     "evidence_identity": [{key: item.get(key) for key in
                                            ("library", "version", "index_target_id", "file_id", "version_id")
                                            if key in item} for item in allowed_evidence],
                     "rpc": allowed.get("rpc")},
                    {"case": f"{label}-unprivileged-B", "http_status": denied["http_status"],
                     "request_id": denied_tool.get("request_id"), "evidence_count": len(denied_evidence),
                     "marker_leaked": any(marker in str(item.get("text", "")) for item in denied_evidence),
                     "session_sha256": session.session_hash, "error": denied_tool.get("error"),
                     "evidence_identity": [{key: item.get(key) for key in
                                            ("library", "version", "index_target_id", "file_id", "version_id")
                                            if key in item} for item in denied_evidence],
                     "rpc": denied.get("rpc")},
                ])
                write_json(self.output / "controlled-unprivileged-identity.json", {
                    "authorization_path": "reader B personal credential; no grants",
                    "authorized_session_sha256": authorized.session_hash,
                    "unprivileged_session_sha256": session.session_hash,
                    "scenario": "one authorized A request and one unprivileged B request sent concurrently per link using distinct sessions",
                    "checks": checks, "passed": None, "partial": True})
                if (not identity_match or denied.get("http_status") != 200
                        or denied_evidence or not denied_tool.get("error")):
                    write_json(self.output / "controlled-unprivileged-identity.json", {
                        "authorization_path": "reader B personal credential; no grants",
                        "authorized_session_sha256": authorized.session_hash,
                        "unprivileged_session_sha256": session.session_hash,
                        "scenario": "one authorized A request and one unprivileged B request sent concurrently per link using distinct sessions",
                        "checks": checks, "passed": False,
                        "failure": "concurrent_identity_isolation_check_failed"})
                    raise RuntimeError("concurrent_identity_isolation_check_failed")
            result = {"authorization_path": "reader B personal credential; no grants",
                      "authorized_session_sha256": authorized.session_hash,
                      "unprivileged_session_sha256": session.session_hash,
                      "scenario": "one authorized A request and one unprivileged B request sent concurrently per link using distinct sessions",
                    "checks": checks, "passed": True, "partial": False}
            write_json(self.output / "controlled-unprivileged-identity.json", result)
            return result
        finally:
            if authorized is not None:
                await authorized.close()
            await session.close()

    @staticmethod
    def wait_job(admin: httpx.Client, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + HTTP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            response = admin.get(f"/api/jobs/{quote(job_id, safe='')}")
            if response.status_code != 200:
                raise RuntimeError(f"managed_job_http_{response.status_code}")
            value = response.json()
            if value.get("status") in TERMINAL_JOBS:
                return value
            time.sleep(0.1)
        raise TimeoutError("managed_job_timeout")

    async def open_sessions(self, token: str, label: str, count: int) -> list[McpHttpSession]:
        url = f"http://127.0.0.1:{self.state['ports']['mcp']}"
        sessions = await asyncio.gather(*(McpHttpSession.open(url, token, label, index)
                                          for index in range(count)))
        for item in sessions:
            item.bind_token(token)
        self.sessions.extend(sessions)
        return sessions

    @staticmethod
    def percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return round(ordered[index], 6)

    def summarize(self, label: str, concurrency: int, rows: list[dict[str, Any]],
                  started: float, ended: float) -> dict[str, Any]:
        elapsed = max(ended - started, 1e-9)
        latencies = [row["latency_seconds"] for row in rows if row.get("latency_seconds") is not None]
        successful_latencies = [row["latency_seconds"] for row in rows
                                if row.get("success") and row.get("latency_seconds") is not None]
        successes = sum(row.get("success") is True for row in rows)
        failures = len(rows) - successes
        errors = Counter(row.get("error_category", "none") for row in rows if not row.get("success"))
        http_statuses = Counter(str(row.get("http_status")) for row in rows
                                if row.get("http_status") is not None)
        mcp_statuses = Counter(str(row.get("mcp_is_error")) for row in rows
                              if row.get("mcp_is_error") is not None)
        stage_samples: dict[str, list[float]] = {}
        start_times = [row.get("wire_start_utc") or row.get("start_utc") or row.get("scheduled_utc")
                       for row in rows]
        end_times = [row.get("end_utc") for row in rows]
        start_times = [value for value in start_times if isinstance(value, str)]
        end_times = [value for value in end_times if isinstance(value, str)]
        for row in rows:
            timings = row.get("server_timing_seconds", {})
            if not timings and isinstance(row.get("server_retrieval"), dict):
                timings = row["server_retrieval"].get("timing_seconds", {})
            for key, value in timings.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    stage_samples.setdefault(key, []).append(float(value))
        summary = {"link": label, "concurrency": concurrency, "requests": len(rows),
                   "successes": successes, "failures": failures,
                   "failure_ratio": round(failures / max(len(rows), 1), 6),
                   "error_categories": dict(errors), "http_status_counts": dict(http_statuses),
                   "mcp_is_error_counts": dict(mcp_statuses), "duration_seconds": round(elapsed, 6),
                   "requests_per_second": round(successes / elapsed, 6),
                   "total_requests_per_second": round(len(rows) / elapsed, 6),
                   "latency_seconds": {"p50_empirical": self.percentile(latencies, .50),
                                       "p95_empirical": self.percentile(latencies, .95),
                                       "p99_empirical": self.percentile(latencies, .99),
                                       "minimum": min(latencies) if latencies else None,
                                       "maximum": max(latencies) if latencies else None,
                                       "percentile_note": "all measured requests including HTTP/MCP/client failures; nearest-rank empirical"},
                   "successful_latency_seconds": {
                       "p50_empirical": self.percentile(successful_latencies, .50),
                       "p95_empirical": self.percentile(successful_latencies, .95),
                       "p99_empirical": self.percentile(successful_latencies, .99),
                       "minimum": min(successful_latencies) if successful_latencies else None,
                       "maximum": max(successful_latencies) if successful_latencies else None,
                       "percentile_note": "nearest-rank empirical; n=16 p99 equals sample maximum"},
                   "server_timing_seconds_empirical_p50": {
                       key: self.percentile(values, .50) for key, values in stage_samples.items()},
                   "server_timing_semantics": "Only MCP response timing fields; not subtracted from end-to-end latency; residual is not inferred queue time.",
                   "start_utc": min(start_times) if start_times else utc_now(),
                   "end_utc": max(end_times) if end_times else utc_now(),
                   "time_window_semantics": "minimum wire_start_utc and maximum end_utc across all request rows"}
        self.phase_summaries.append(summary)
        return summary

    async def run_level(self, label: str, sessions: list[McpHttpSession], arguments: dict[str, Any],
                        concurrency: int, request_count: int, *, phase: str) -> dict[str, Any]:
        if self.sampler.stop_reason:
            return {"link": label, "concurrency": concurrency, "skipped": self.sampler.stop_reason}
        rows: list[dict[str, Any]] = []
        semaphore = asyncio.Semaphore(concurrency)
        async def one(index: int) -> None:
            if self.sampler.stop_reason:
                return
            session = sessions[index]
            request_tag = f"{phase}-{label}-{concurrency}-{index:02d}"
            scheduled_mono = time.monotonic()
            scheduled_utc = utc_now()
            async with semaphore:
                wire_started_mono = time.monotonic()
                wire_started_utc = utc_now()
                wire_started = time.perf_counter()
                try:
                    expected_marker = arguments.get("_expected_marker", "")
                    wire_arguments = {key: value for key, value in arguments.items()
                                      if not key.startswith("_")}
                    result = await session.call("tools/call", {"name": "search_docs", "arguments": wire_arguments})
                    elapsed = time.perf_counter() - wire_started
                    tool = result["tool"]
                    http_status = result["http_status"]
                    rpc_error = result["rpc"].get("error") if isinstance(result.get("rpc"), dict) else None
                    status = result["rpc"].get("result", {}).get("isError") if isinstance(result.get("rpc"), dict) else None
                    evidence = tool.get("evidence") if isinstance(tool, dict) else None
                    evidence = evidence if isinstance(evidence, list) else []
                    marker_ok = any(expected_marker in str(item.get("text", "")) for item in evidence) if expected_marker else bool(evidence)
                    if label == "shared" and evidence:
                        marker_ok = marker_ok and all(
                            item.get("library") == "synthetic-concurrency"
                            and item.get("version") == "1.0"
                            and item.get("index_target_id") == "synthetic-concurrency-1.0-target"
                            for item in evidence
                        )
                    if label == "managed" and evidence:
                        identity = tool.get("managed_file", {})
                        marker_ok = marker_ok and identity.get("file_id") == arguments.get("file_id")
                        marker_ok = marker_ok and identity.get("version_id") == arguments.get("_expected_version_id")
                        marker_ok = marker_ok and all(
                            item.get("file_id") == arguments.get("file_id")
                            and item.get("version_id") == identity.get("version_id")
                            and item.get("version") == identity.get("version")
                            for item in evidence
                        )
                    error_category = None
                    if http_status != 200:
                        error_category = f"http_{http_status}"
                    elif rpc_error:
                        error_category = "jsonrpc_error"
                    elif status or tool.get("error"):
                        error_category = str(tool.get("error", "mcp_tool_error"))
                    elif not marker_ok:
                        error_category = "expected_evidence_missing"
                    row = {"case_id": request_tag, "link": label, "phase": phase,
                           "concurrency": concurrency, "worker": f"client-{index:02d}",
                           "session_sha256": session.session_hash,
                           "scheduled_utc": scheduled_utc, "wire_start_utc": wire_started_utc,
                           "end_utc": utc_now(), "scheduled_monotonic": round(scheduled_mono, 6),
                           "wire_start_monotonic": round(wire_started_mono, 6),
                           "latency_seconds": round(elapsed, 6), "http_status": http_status,
                           "jsonrpc_error_code": rpc_error.get("code") if isinstance(rpc_error, dict) else None,
                           "mcp_is_error": status, "request_id": tool.get("request_id"),
                           "success": error_category is None, "error_category": error_category,
                           "evidence_count": len(evidence), "expected_marker_match": marker_ok,
                           "synthetic_query": wire_arguments.get("query"),
                           "synthetic_evidence_text": [item.get("text", "") for item in evidence],
                           "evidence_identity": [{k: item.get(k) for k in
                                                  ("library", "version", "index_target_id", "file_id", "version_id", "source_path")
                                                  if k in item} for item in evidence],
                           "server_timing_seconds": tool.get("timing_seconds", {})
                           if isinstance(tool.get("timing_seconds"), dict) else {},
                           "server_retrieval": tool.get("retrieval")
                           if isinstance(tool.get("retrieval"), dict) else {}}
                except asyncio.CancelledError:
                    row = {"case_id": request_tag, "link": label, "phase": phase,
                           "concurrency": concurrency, "worker": f"client-{index:02d}",
                           "session_sha256": session.session_hash, "scheduled_utc": scheduled_utc,
                           "wire_start_utc": locals().get("wire_started_utc", scheduled_utc),
                           "end_utc": utc_now(), "success": False,
                           "error_category": "client_task_cancelled", "server_observed_cancel": False}
                except Exception as exc:  # noqa: BLE001 - category only; no error text/body/token.
                    row = {"case_id": request_tag, "link": label, "phase": phase,
                           "concurrency": concurrency, "worker": f"client-{index:02d}",
                           "session_sha256": session.session_hash, "scheduled_utc": scheduled_utc,
                           "wire_start_utc": locals().get("wire_started_utc", scheduled_utc),
                           "end_utc": utc_now(), "success": False,
                           "error_category": f"client_{type(exc).__name__}"}
                finally:
                    row.setdefault("latency_seconds", round(time.perf_counter() - wire_started, 6)
                                   if "wire_started" in locals() else None)
                    row.setdefault("http_status", None)
                    row.setdefault("mcp_is_error", None)
                    row.setdefault("wire_start_monotonic", locals().get("wire_started_mono"))
                    row.setdefault("scheduled_monotonic", round(scheduled_mono, 6))
                rows.append(row)
                self.records.append(row)
                append_jsonl(self.output / "requests.jsonl", row)
        count = min(request_count, len(sessions))
        started = time.perf_counter()
        await asyncio.gather(*(one(index) for index in range(count)))
        ended = time.perf_counter()
        summary = self.summarize(label, concurrency, rows, started, ended)
        summary["phase"] = phase
        summary["sessions"] = count
        summary["same_session_concurrent_calls"] = False
        summary["in_flight_limit"] = concurrency
        write_json(self.output / f"phase-{phase}-{label}-{concurrency}.json", summary)
        if summary["failure_ratio"] >= FAILURE_STOP_RATIO:
            self.stop_event.set()
        return summary

    async def run_collision(self, shared: McpHttpSession, managed: McpHttpSession,
                            shared_args: dict[str, Any], managed_args: dict[str, Any]) -> dict[str, Any]:
        start = time.perf_counter()
        shared_wire = {key: value for key, value in shared_args.items() if not key.startswith("_")}
        managed_wire = {key: value for key, value in managed_args.items() if not key.startswith("_")}
        results = await asyncio.gather(
            shared.call("tools/call", {"name": "search_docs", "arguments": shared_wire}),
            managed.call("tools/call", {"name": "search_docs", "arguments": managed_wire}),
            return_exceptions=True,
        )
        rows = []
        for name, result in zip(("shared", "managed"), results):
            if isinstance(result, BaseException):
                rows.append({"link": name, "success": False, "error_category": type(result).__name__})
            else:
                tool = result.get("tool", {})
                evidence = tool.get("evidence", []) if isinstance(tool, dict) else []
                expected_args = shared_args if name == "shared" else managed_args
                marker = expected_args.get("_expected_marker", "")
                marker_match = bool(evidence) and all(marker in str(item.get("text", ""))
                                                      for item in evidence)
                if name == "shared":
                    identity_match = bool(evidence) and all(
                        item.get("library") == "synthetic-concurrency"
                        and item.get("version") == "1.0"
                        and item.get("index_target_id") == shared_args.get("target_id")
                        for item in evidence)
                else:
                    identity = tool.get("managed_file", {}) if isinstance(tool, dict) else {}
                    identity_match = (bool(evidence)
                                      and identity.get("file_id") == managed_args.get("file_id")
                                      and identity.get("version_id") == managed_args.get("_expected_version_id")
                                      and all(item.get("file_id") == managed_args.get("file_id")
                                              and item.get("version_id") == managed_args.get("_expected_version_id")
                                              for item in evidence))
                busy_rejection = (result.get("http_status") == 503
                                  and result.get("http_error_category") == "model_busy")
                passed = (busy_rejection or (result.get("http_status") == 200
                                             and marker_match and identity_match
                                             and not tool.get("error")))
                rows.append({"link": name, "http_status": result.get("http_status"),
                             "request_id": tool.get("request_id") if isinstance(tool, dict) else None,
                             "evidence_count": len(evidence), "expected_marker_match": marker_match,
                             "expected_identity_match": identity_match, "model_busy_rejection": busy_rejection,
                             "success": passed,
                             "error_category": None if passed else (result.get("http_error_category")
                                                                    if result.get("http_status") != 200
                                                                    else "tool_error" if isinstance(tool, dict) and tool.get("error")
                                                                    else "collision_evidence_identity_mismatch"),
                             "mcp_tool_error": tool.get("error") if isinstance(tool, dict) else None,
                             "mcp_rpc": result.get("rpc"),
                             "raw_response_text": result.get("raw_response_text"),
                             "transport_parse_error": result.get("transport_parse_error"),
                             "http_error_category": result.get("http_error_category"),
                             "session_sha256": (shared if name == "shared" else managed).session_hash,
                             "evidence_identity": [{key: item.get(key) for key in
                                                    ("library", "version", "index_target_id", "file_id", "version_id")
                                                    if key in item} for item in evidence],
                             "server_timing_seconds": tool.get("timing_seconds", {})
                             if isinstance(tool, dict) else {}})
        value = {"scenario": "one-shared-and-one-managed-simultaneous-request",
                 "elapsed_seconds": round(time.perf_counter() - start, 6),
                 "results": rows,
                 "passed": all(row.get("success", False) for row in rows),
                 "interpretation": "controlled single collision only; model busy may return 503; no saturation claim"}
        write_json(self.output / "collision.json", value)
        return value

    async def close_sessions(self) -> None:
        await asyncio.gather(*(session.close() for session in self.sessions), return_exceptions=True)


def parse_rpc_tool_response(result: dict[str, Any]) -> dict[str, Any]:
    return result.get("tool", {}) if isinstance(result.get("tool"), dict) else {}


def run_benchmark(state: dict[str, Any], *, single_only: bool = False, controls_only: bool = False) -> int:
    output = Path(state["evidence_dir"])
    output.mkdir(parents=True, exist_ok=True)
    model_token = Path(state["model_secret_path"]).read_text(encoding="ascii").strip()
    admin_token = Path(state["admin_secret_path"]).read_text(encoding="ascii").strip()
    if source_tree_hash() != state["source_sha256"]:
        raise RuntimeError("source_changed_after_prepare")
    before = resource_snapshot()
    if before.get("host_memory_free_mib", 0) < RAM_START_FREE_MIB or not before.get("gpu") or before["gpu"]["free_mib"] < GPU_START_FREE_MIB:
        raise RuntimeError("resource_start_gate_rejected")
    compose = compose_args(state)
    sampler = Sampler(state, output / "resources.jsonl")
    stop_event = threading.Event()
    monitor = threading.Thread(target=lambda: monitor_stop(sampler, stop_event), daemon=True)
    benchmark: Benchmark | None = None
    admin: httpx.Client | None = None
    reader: dict[str, Any] | None = None
    reader_b: dict[str, Any] | None = None
    admin_password = "A9!" + uuid.uuid4().hex + "Z"
    run_success = False
    failure_stage = "preflight"
    cleanup_result: dict[str, Any] = {"attempted": False}
    try:
        # Sample from before the first container starts so model load and fixture
        # embedding are covered by the same resource timeline as the MCP matrix.
        sampler.start()
        monitor.start()
        # Seed only this run's disposable identity database before the backend
        # initializes it. Calling the HTTP bootstrap through Docker's published
        # port would correctly fail the service's loopback-only guard.
        bootstrap_isolated_identity_db(
            Path(state["runtime_root"]) / "identity" / "identity.sqlite", admin_password)
        failure_stage = "start_model_and_qdrant"
        # Only Qdrant + the single model-node service start first. No production
        # project name, volume, data directory, .env file or service is referenced.
        model_start = time.perf_counter()
        run_command(compose + ["up", "-d", "--no-build", "--pull", "never", "qdrant", "model-node"], timeout=180)
        wait_http(f"http://127.0.0.1:{state['ports']['model']}/v1/health",
                  headers={"Authorization": f"Bearer {model_token}"}, expect_json=True,
                  timeout_seconds=900, stop_event=stop_event)
        model_identity = wait_http(f"http://127.0.0.1:{state['ports']['model']}/v1/identity",
                                   headers={"Authorization": f"Bearer {model_token}"},
                                   expect_json=True, timeout_seconds=10)
        if model_identity.get("embedding", {}).get("model_id") != EMBEDDING_ID or model_identity.get("reranker", {}).get("model_id") != RERANKER_ID:
            raise RuntimeError("model_identity_mismatch")
        model_startup_seconds = round(time.perf_counter() - model_start, 6)
        # Seed a shared corpus with vectors produced through the one real, read-only
        # GPU model-node process, before MCP lifespan reads index-registry.json.
        # This is fixture preparation, separate from MCP workload timing.
        shared_fixture = seed_shared_fixture(state, model_token)
        if sampler.stop_reason or stop_event.is_set():
            raise RuntimeError("resource_safety_gate_stopped_before_mcp_start")
        failure_stage = "start_management_and_mcp"
        compose_started = time.perf_counter()
        run_command(compose + ["up", "-d", "--no-build", "--pull", "never", "management", "mcp"], timeout=180)
        admin_base = f"http://127.0.0.1:{state['ports']['admin']}"
        wait_http(admin_base + "/api/health", timeout_seconds=180, stop_event=stop_event)
        startup_seconds = round(time.perf_counter() - compose_started, 6)
        benchmark = Benchmark(state, output, sampler, stop_event)
        failure_stage = "identity_setup"
        admin, admin_headers = benchmark.admin_login(admin_base, admin_password)
        reader = benchmark.create_managed_reader(admin, admin_headers)
        benchmark.reader_token = reader["mcp_token"]
        reader_b = benchmark.create_unprivileged_reader(admin, admin_headers)
        failure_stage = "managed_fixture_upload"
        managed_fixture = benchmark.upload_managed(
            admin, admin_headers, reader, "synthetic_managed_marker_v1")
        # Permission baseline precedes every grant. Once that proof is saved, grant
        # this reader access to the shared synthetic library and managed file.
        asyncio.run(benchmark.verify_default_deny(
            f"http://127.0.0.1:{state['ports']['mcp']}", reader["mcp_token"],
            managed_fixture["file_id"]))
        managed_fixture["grant_results"] = benchmark.grant_resources(
            admin, admin_headers, reader, managed_fixture["file_id"], shared_fixture["library"])
        failure_stage = "authorized_identity_concurrency_probe"
        asyncio.run(benchmark.verify_unprivileged_identity(
            f"http://127.0.0.1:{state['ports']['mcp']}", reader_b["mcp_token"],
            shared_fixture["target_id"], managed_fixture["file_id"],
            shared_fixture["marker"], managed_fixture["marker"]))
        source_attestation = [attest_loaded_source(compose, service, state["source_sha256"])
                             for service in ("management", "mcp", "model-node")]
        run_command(compose + ["ps", "--format", "json"], timeout=15)
        image_manifest = {service: docker_image_id(image) for service, image in
                          (("backend", BACKEND_IMAGE), ("model-node", MODEL_IMAGE))}
        manifest = {"run_id": state["run_id"], "project": state["project"],
                    "started_utc": utc_now(), "git_revision": run_command(["git", "rev-parse", "HEAD"]),
                    "source_tree_sha256": source_tree_hash(), "images": image_manifest,
                    "runtime_source_attestation": source_attestation,
                    "model_identity": {"embedding_model_id": model_identity["embedding"]["model_id"],
                                       "embedding_asset_sha256": model_identity["embedding"]["asset_sha256"],
                                       "reranker_model_id": model_identity["reranker"]["model_id"],
                                       "reranker_asset_sha256": model_identity["reranker"]["asset_sha256"],
                                       "implementation_sha256": model_identity["implementation_sha256"]},
                    "readonly_weights": True, "one_gpu_model_node_process": True,
                    "model_and_qdrant_cold_start_seconds": model_startup_seconds,
                    "management_and_mcp_cold_start_seconds": startup_seconds,
                    "cold_scope": "new Qdrant/model/management/MCP containers and model process; host Docker VM cache and Windows filesystem cache not cold",
                    "shared_fixture": shared_fixture,
                    "managed_fixture": {k: v for k, v in managed_fixture.items()},
                    "identity_scenario": "reader A authorized; reader B valid personal credential with no grants; for both shared and managed links, one A request and one B request were sent concurrently and B returned no evidence before load",
                    "timing_contract": "MCP endpoint reports named stages; no end-to-end minus stage queue inference",
                    "protocol": PROTOCOL,
                    "requests_per_concurrency_level_per_link": REQUESTS_PER_LEVEL,
                    "timeout_seconds": HTTP_TIMEOUT_SECONDS,
                    "resource_thresholds": {"host_free_memory_stop_mib": RAM_STOP_MIB,
                                            "gpu_free_memory_stop_mib": GPU_FREE_STOP_MIB,
                                            "failure_ratio_stop": FAILURE_STOP_RATIO}}
        write_json(output / "run-manifest.json", manifest)
        benchmark = Benchmark(state, output, sampler, stop_event)
        benchmark.admin = admin
        benchmark.admin_headers = admin_headers
        benchmark.reader = reader
        # Permission denial is checked before granting, then the benchmark user gets
        # a separate MCP credential. The measured managed requests are authorized.
        async def measured() -> None:
            session_count = 1 if single_only or controls_only else REQUESTS_PER_LEVEL
            shared_sessions = await benchmark.open_sessions(reader["mcp_token"], "shared", session_count)
            managed_sessions = await benchmark.open_sessions(reader["mcp_token"], "managed", session_count)
            shared_arguments = {"query": "synthetic concurrency evidence", "target_id": shared_fixture["target_id"],
                                "top_k": 1, "_expected_marker": shared_fixture["marker"]}
            managed_arguments = {"query": "synthetic managed marker", "file_id": managed_fixture["file_id"],
                                 "top_k": 1, "_expected_marker": managed_fixture["marker"],
                                 "_expected_version_id": managed_fixture["version_id"]}
            # Each connection performs its own initialize/initialized/tools/list and
            # has a unique MCP session. A single request per link precedes the matrix.
            links = (("shared", shared_sessions, shared_arguments),
                     ("managed", managed_sessions, managed_arguments))
            # First request and warm-up are balanced across both routes before
            # either link advances to a higher concurrency level.
            for link, sessions, arguments in links:
                await benchmark.run_level(link, sessions, arguments, 1, 1, phase="single-first-request")
                if benchmark.stop_event.is_set() or sampler.stop_reason or stop_event.is_set():
                    return
            if single_only:
                await benchmark.close_sessions()
                return
            for link, sessions, arguments in links:
                await benchmark.run_level(link, sessions, arguments, 1, 1, phase="warm-up")
                if benchmark.stop_event.is_set() or sampler.stop_reason or stop_event.is_set():
                    return
            if controls_only:
                if not sampler.stop_reason and not stop_event.is_set():
                    await benchmark.run_collision(shared_sessions[0], managed_sessions[0],
                                                  shared_arguments, managed_arguments)
                await benchmark.close_sessions()
                return
            for level in CONCURRENCY_LEVELS:
                for link, sessions, arguments in links:
                    if sampler.stop_reason or stop_event.is_set():
                        return
                    summary = await benchmark.run_level(link, sessions, arguments, level,
                                                        REQUESTS_PER_LEVEL, phase="matrix")
                    if summary.get("failure_ratio", 0) >= FAILURE_STOP_RATIO or sampler.stop_reason or stop_event.is_set():
                        return
                await asyncio.sleep(1)
            if not single_only and not sampler.stop_reason and not stop_event.is_set():
                await benchmark.run_collision(shared_sessions[0], managed_sessions[0],
                                              shared_arguments, managed_arguments)
            await benchmark.close_sessions()
        failure_stage = "single_and_concurrency_measurement"
        asyncio.run(measured())
        run_success = not sampler.stop_reason and not stop_event.is_set()
        controlled_results: dict[str, Any] = {}
        if (not single_only or controls_only) and run_success and admin is not None and reader is not None and managed_fixture:
            try:
                from controlled_scenarios import run_managed_lifecycle

                managed_revision = next(item["revision"] for item in managed_fixture["grant_results"]
                                        if item["resource_kind"] == "managed_file")
                lifecycle = run_managed_lifecycle(
                    admin=admin, admin_headers=admin_headers,
                    mcp_base_url=f"http://127.0.0.1:{state['ports']['mcp']}",
                    library_id=managed_fixture["library_id"], file_id=managed_fixture["file_id"],
                    user_id=reader["user_id"], current_grant_revision=managed_revision,
                    query="synthetic managed marker", reader_token=reader["mcp_token"],
                    session_factory=McpHttpSession.open, wait_job=Benchmark.wait_job,
                    output_dir=output)
                run_success = bool(lifecycle.get("passed"))
                controlled_results["managed_lifecycle"] = lifecycle
            except Exception as exc:  # Keep failure diagnostics secret-free and actionable.
                failure_path = output / "controlled-managed-lifecycle-failure.json"
                if not failure_path.exists():
                    import traceback
                    write_json(failure_path, {
                        "scenario": "managed-replace-revoke-delete", "passed": False,
                        "error_code": "managed_lifecycle_uncaught:" + type(exc).__name__,
                        "trace_frames": [{"file": Path(item.filename).name, "line": item.lineno,
                                          "function": item.name}
                                         for item in traceback.extract_tb(exc.__traceback__)],
                        "recorded_utc": utc_now()})
                run_success = False
        if (not single_only or controls_only) and not sampler.stop_reason and not stop_event.is_set() and reader is not None:
            try:
                from controlled_scenarios import probe_mcp_cancellation

                controlled_results["cancellation"] = asyncio.run(probe_mcp_cancellation(
                    base_url=f"http://127.0.0.1:{state['ports']['mcp']}",
                    token=reader["mcp_token"], session_factory=McpHttpSession.open,
                    query="synthetic concurrency evidence", target_id=shared_fixture["target_id"],
                    expected_marker=shared_fixture["marker"], cancel_after_ms=1,
                    output=output / "controlled-cancellation.json"))
            except Exception as exc:
                failure_path = output / "controlled-cancellation-failure.json"
                if not failure_path.exists():
                    import traceback
                    write_json(failure_path, {
                        "scenario": "stateful-MCP-cancellation-probe", "passed": False,
                        "server_work_stopped": "unknown",
                        "error_code": "cancellation_probe_uncaught:" + type(exc).__name__,
                        "trace_frames": [{"file": Path(item.filename).name, "line": item.lineno,
                                          "function": item.name}
                                         for item in traceback.extract_tb(exc.__traceback__)],
                        "recorded_utc": utc_now()})
        write_json(output / "summary.json", {
            "status": ("controls_only_completed" if controls_only and run_success else
                       "controls_only_failed" if controls_only else
                       "completed" if run_success else "failed_or_stopped"),
            "benchmark_status": "skipped_controls_only" if controls_only else ("completed" if run_success else "failed_or_stopped"),
            "controls_status": "completed" if controlled_results and all(
                item.get("passed", False) for item in controlled_results.values()) else
                "failed_or_incomplete" if controls_only or controlled_results else "not_run",
            "stop_reason": sampler.stop_reason,
            "request_failure_threshold_reached": bool(stop_event.is_set() and not sampler.stop_reason),
            "phase_summaries": benchmark.phase_summaries,
            "request_count": len(benchmark.records),
            "errors": dict(Counter(row.get("error_category", "none") for row in benchmark.records
                                    if not row.get("success"))),
            "percentile_scope": "exploratory empirical percentiles only; 16 samples per level; no SLO",
            "controlled_scenarios": controlled_results,
            "server_cancel": "MCP cancellation notification is observed; synchronous thread/model work termination is unknown",
            "ended_utc": utc_now(),
        })
        write_json(output / "requests.json", benchmark.records)
    except Exception as exc:
        try:
            import traceback

            frames = []
            for frame in traceback.extract_tb(exc.__traceback__):
                frame_path = Path(frame.filename)
                try:
                    safe_path = frame_path.resolve().relative_to(REPO.resolve()).as_posix()
                except (OSError, ValueError):
                    safe_path = frame_path.name
                frames.append({"file": safe_path, "line": frame.lineno, "function": frame.name})
            write_json(output / "harness-failure.json", {
                "status": "failed", "failure_stage": failure_stage,
                "error_category": type(exc).__name__, "stack_frames": frames,
                "recorded_utc": utc_now(),
                "exception_text_omitted": True,
            })
        except Exception:
            pass
        raise
    finally:
        if admin is not None:
            admin.close()
        if benchmark is not None:
            try:
                asyncio.run(benchmark.close_sessions())
            except RuntimeError:
                pass
        # Capture redacted service diagnostics while containers still exist; down
        # removes both containers and the isolated Qdrant named volume.
        if sampler.thread:
            sampler.stop()
        redact_logs_and_remove_secrets(state, output, model_token, admin_token,
                                       reader.get("mcp_token", "") if reader else "",
                                       reader_b.get("mcp_token", "") if reader_b else "",
                                       reader.get("reader_password", "") if reader else "",
                                       reader_b.get("reader_password", "") if reader_b else "",
                                       admin_password)
        if state.get("project"):
            cleanup_result["attempted"] = True
            try:
                run_command(compose + ["down", "--volumes", "--remove-orphans", "--timeout", "30"], timeout=90)
                cleanup_result["compose_down"] = "completed"
            except Exception:
                cleanup_result["compose_down"] = "failed"
            cleanup_result.update(verify_no_project_resources(state["project"]))
        if source_tree_hash() != state["source_sha256"]:
            cleanup_result["source_changed_during_run"] = True
        cleanup_clean = (cleanup_result.get("compose_down") == "completed"
                         and cleanup_result.get("resources_remaining") is False)
        if cleanup_clean:
            try:
                shutil.rmtree(Path(state["runtime_root"]))
                cleanup_result["temporary_root_removed"] = True
            except OSError:
                cleanup_result["temporary_root_removed"] = False
            if cleanup_result.get("temporary_root_removed"):
                Path(state["env_file"]).unlink(missing_ok=True)
                Path(state["state_file"]).unlink(missing_ok=True)
                Path(state["overlay_file"]).unlink(missing_ok=True)
        else:
            cleanup_result["temporary_root_removed"] = False
            cleanup_result["recovery_state_file"] = state["state_file"]
            cleanup_result["recovery_runtime_root"] = state["runtime_root"]
            cleanup_result["credentials_removed"] = False
        write_json(output / "cleanup.json", cleanup_result)
    return 0 if run_success else 2


def monitor_stop(sampler: Sampler, stop_event: threading.Event) -> None:
    while not stop_event.is_set() and not sampler.stop_event.is_set():
        time.sleep(0.1)
    if sampler.stop_reason:
        stop_event.set()


def verify_no_project_resources(project: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label, args in (("containers", ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]),
                        ("volumes", ["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"]),
                        ("networks", ["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"])):
        try:
            found = run_command(args, timeout=15).splitlines()
            result[label] = len([item for item in found if item.strip()])
        except Exception:
            result[label] = "inspection_failed"
    result["resources_remaining"] = any(isinstance(result[key], int) and result[key] > 0
                                         for key in ("containers", "volumes", "networks"))
    return result


def redact_logs_and_remove_secrets(state: dict[str, Any], output: Path, *secrets_to_redact: str) -> None:
    compose = compose_args(state)
    try:
        raw = run_command(compose + ["logs", "--no-color", "--timestamps", "qdrant", "management", "mcp", "model-node"], timeout=30)
    except Exception:
        raw = "log_capture_failed"
    known = [item for item in secrets_to_redact if item]
    sanitized = []
    patterns = [re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
                re.compile(r"(?i)(token|password|secret)(\s*[:=]\s*)[^\s,;]+")]
    for line in raw.splitlines():
        value = line
        for secret in known:
            value = value.replace(secret, "[REDACTED]")
        for pattern in patterns:
            value = pattern.sub(r"\1[REDACTED]", value)
        # Keep service logs only as diagnostic lines; generated corpus markers and
        # request text never belong in operational artifacts.
        sanitized.append(value[:2000])
    write_json(output / "service-log-diagnostics.json", {
        "captured_utc": utc_now(), "source": "docker compose logs",
        "line_count_total": len(raw.splitlines()), "line_count_redacted": len(sanitized),
        "lines_redacted": sanitized,
        "queries_and_credentials_recorded": False,
    })


def cleanup_state(path: Path) -> int:
    state = read_state(path)
    compose = compose_args(state)
    down_ok = False
    try:
        run_command(compose + ["down", "--volumes", "--remove-orphans", "--timeout", "30"], timeout=90)
        down_ok = True
    finally:
        audit = verify_no_project_resources(state["project"])
        clean = down_ok and audit.get("resources_remaining") is False
        if clean:
            shutil.rmtree(Path(state["runtime_root"]), ignore_errors=True)
            Path(state["env_file"]).unlink(missing_ok=True)
            Path(state["overlay_file"]).unlink(missing_ok=True)
            Path(state["state_file"]).unlink(missing_ok=True)
        else:
            audit["recovery_state_file"] = state["state_file"]
            audit["runtime_root_preserved"] = state["runtime_root"]
    print(json.dumps({"cleanup": audit,
                      "temporary_root_removed": not Path(state["runtime_root"]).exists()}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare", help="validate and create isolated config; does not start services")
    prepare_parser.add_argument("--embedding-path", type=Path, default=Path("E:/Models/Qwen3-Embedding-0.6B"))
    prepare_parser.add_argument("--reranker-path", type=Path, default=Path("E:/Models/Qwen3-Reranker-0.6B"))
    run_parser = subparsers.add_parser("run", help="start the isolated Docker benchmark and remove it afterward")
    run_parser.add_argument("--state", required=True, type=Path)
    run_parser.add_argument("--single-only", action="store_true",
                            help="run one first request per link, then save/clean; no concurrency matrix")
    run_parser.add_argument("--controls-only", action="store_true",
                            help="run fixture, singles/warmups and controlled contract probes; skip concurrency matrix")
    cleanup_parser = subparsers.add_parser("cleanup", help="remove resources for a prepared isolated project")
    cleanup_parser.add_argument("--state", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args)
            return 0
        if args.command == "cleanup":
            return cleanup_state(args.state.resolve())
        state = read_state(args.state.resolve())
        return run_benchmark(state, single_only=args.single_only, controls_only=args.controls_only)
    except (OSError, RuntimeError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
        # No arbitrary exception text: it can include host paths or environment values.
        print(f"baseline_failed:{type(exc).__name__}:{str(exc).split(':', 1)[0]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
