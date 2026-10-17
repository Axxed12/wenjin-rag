"""Offline delivery manifest, quiesced project backup and owned-container diagnostics.

This is an operator tool, not a service lifecycle manager. It never loads project
settings, reads .env files, starts containers, or chooses paths from the current
working directory implicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

MANIFEST_NAME = "team-delivery-manifest.json"
BACKUP_SCHEMA = 1
OFFLINE_SCHEMA = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ASSET_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._:-]{0,127})@(?P<version>[^=]{1,128})=(?P<path>.+)$")
_SECRET_NAME_RE = re.compile(
    r"^(?:\.env(?:\..*)?|credentials?(?:\..*)?|secrets?|.*\.(?:pem|key|p12|pfx))$",
    re.IGNORECASE,
)
_LOG_SECRET_RE = re.compile(
    r"(?i)(\b(?:authorization|token|password|secret|api[_-]?key|cookie)\b\s*[:=]\s*|\bbearer\s+)([^\s,;]+)"
)


class DeliveryError(ValueError):
    """Invalid or unsafe delivery input."""


def _safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise DeliveryError("路径必须是非空的POSIX相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DeliveryError("路径不得为空、绝对路径或包含上级目录")
    if path.as_posix() != value:
        raise DeliveryError("路径必须使用规范POSIX相对格式")
    if path.as_posix().casefold() == MANIFEST_NAME.casefold():
        raise DeliveryError("路径占用了工具保留的manifest名称")
    for part in path.parts:
        if (part.endswith((".", " ")) or any(ord(char) < 32 or char in '<>"|?*' for char in part)
                or re.match(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", part)):
            raise DeliveryError("路径包含Windows不安全文件名")
    if path.parts[0].lower() in {".git", ".venv", "secrets"}:
        raise DeliveryError("不允许打包版本库、虚拟环境或secret目录")
    if any(_SECRET_NAME_RE.fullmatch(part) for part in path.parts):
        raise DeliveryError("不允许打包.env、凭据文件或私钥文件")
    if any(part.lower().endswith(("-wal", "-shm")) for part in path.parts):
        raise DeliveryError("SQLite WAL sidecar由一致性快照替代，不能直接打包")
    return path


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ensure_regular_source(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise DeliveryError(f"资产必须是普通文件，且不能是符号链接：{path.name}")


def _ensure_no_symlink_ancestors(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise DeliveryError(f"资产路径包含符号链接：{part}")
    return current


def _walk_component(root: Path, relative: PurePosixPath) -> list[tuple[PurePosixPath, Path]]:
    source = root.joinpath(*relative.parts)
    if source.is_symlink():
        raise DeliveryError(f"不允许符号链接组件：{relative.as_posix()}")
    if not source.exists():
        raise DeliveryError(f"组件不存在：{relative.as_posix()}")
    if source.is_file():
        return [(relative, source)]
    if not source.is_dir():
        raise DeliveryError(f"组件必须是普通文件或目录：{relative.as_posix()}")

    result: list[tuple[PurePosixPath, Path]] = []
    for current, directories, files in os.walk(source, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            child = current_path / name
            rel = PurePosixPath(child.relative_to(root).as_posix())
            _safe_relative(rel.as_posix())
            if child.is_symlink():
                raise DeliveryError(f"不允许目录符号链接：{rel.as_posix()}")
        for name in files:
            child = current_path / name
            rel = PurePosixPath(child.relative_to(root).as_posix())
            _safe_relative(rel.as_posix())
            _ensure_regular_source(child)
            result.append((rel, child))
    return sorted(result, key=lambda item: item[0].as_posix().casefold())


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    """Use SQLite's backup API so committed WAL data is folded into one file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_db = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
    target_db = sqlite3.connect(destination)
    try:
        source_db.backup(target_db, pages=256, sleep=0.01)
        result = target_db.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise DeliveryError(f"SQLite一致性检查失败：{source.name}")
        target_db.commit()
    finally:
        target_db.close()
        source_db.close()


def create_backup_bundle(project_root: Path, components: list[str], output: Path) -> dict[str, Any]:
    """Create a hash-indexed zip from explicitly named relative components."""
    root = Path(project_root).resolve(strict=True)
    if not root.is_dir() or not components:
        raise DeliveryError("project_root必须是目录且至少指定一个组件")
    output = Path(output).absolute()
    if output.exists():
        raise FileExistsError(f"备份输出已存在，拒绝覆盖：{output.name}")
    if output.resolve().is_relative_to(root):
        raise DeliveryError("备份包必须写到项目root之外")

    component_paths = [_safe_relative(item) for item in components]
    expanded: dict[str, Path] = {}
    casefolded: set[str] = set()
    for relative in component_paths:
        for file_relative, source in _walk_component(root, relative):
            name = file_relative.as_posix()
            if name in expanded or name.casefold() in casefolded:
                raise DeliveryError(f"组件有重复或重叠路径：{name}")
            expanded[name] = source
            casefolded.add(name.casefold())
    if not expanded:
        raise DeliveryError("指定组件中没有普通文件")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wenjin-delivery-stage-") as temporary:
        stage = Path(temporary)
        records = []
        for name, source in sorted(expanded.items()):
            relative = _safe_relative(name)
            target = stage.joinpath(*relative.parts)
            _ensure_regular_source(source)
            before = source.stat()
            if source.suffix.lower() in {".sqlite", ".sqlite3", ".db"} and _needs_sqlite_snapshot(name):
                _sqlite_snapshot(source, target)
                snapshot_method = "sqlite-backup-api"
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                snapshot_method = "file-copy"
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise DeliveryError(f"资产在备份期间变化；请停写后重试：{relative.name}")
            records.append({
                "path": name,
                "size_bytes": target.stat().st_size,
                "sha256": _sha256_file(target),
                "snapshot_method": snapshot_method,
            })
        managed_raw_layout = _managed_raw_layout(root, expanded.get("data/file-management/manager.sqlite"))
        manifest = {
            "schema_version": BACKUP_SCHEMA,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_project_root": str(root),
            "components": sorted(item.as_posix() for item in component_paths),
            "managed_raw_layout": managed_raw_layout,
            "files": records,
            "integrity_scope": "sha256-accidental-corruption-detection; not a signature",
            "sqlite_scope": (
                "mutable coordination SQLite=SQLite Backup API; "
                "immutable index/sealed BM25 SQLite=byte copy; "
                "cross-store writers must be quiesced"
            ),
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr(MANIFEST_NAME, manifest_bytes)
                for record in records:
                    archive.write(stage.joinpath(*PurePosixPath(record["path"]).parts), record["path"])
        except Exception:
            output.unlink(missing_ok=True)
            raise
    return {
        "bundle": str(output),
        "bundle_sha256": _sha256_file(output),
        "files": len(records),
        "bytes": output.stat().st_size,
    }


def _needs_sqlite_snapshot(relative_path: str) -> bool:
    """SQLite API snapshot mutable coordination stores, preserve immutable index bytes."""
    normalized = relative_path.replace("\\", "/").casefold()
    return not (
        "/data/indexes/" in f"/{normalized}"
        or "/data/file-management/indexes/" in f"/{normalized}"
        or ("/data/releases/" in f"/{normalized}" and normalized.endswith("/bm25.sqlite"))
    )


def _read_backup_archive(bundle: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    bundle = Path(bundle)
    if bundle.is_symlink() or not bundle.is_file():
        raise DeliveryError("备份包必须是普通文件且不能是符号链接")
    with zipfile.ZipFile(bundle, "r") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or MANIFEST_NAME not in names:
            raise DeliveryError("备份包缺少manifest或包含重复成员")
        manifest_info = archive.getinfo(MANIFEST_NAME)
        if manifest_info.file_size > 4 * 1024 * 1024:
            raise DeliveryError("备份manifest超过大小限制")
        try:
            manifest = json.loads(archive.read(MANIFEST_NAME))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DeliveryError("备份manifest不是有效JSON") from error
        if not isinstance(manifest, dict) or manifest.get("schema_version") != BACKUP_SCHEMA:
            raise DeliveryError("不支持的备份manifest版本")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise DeliveryError("备份manifest文件列表为空或无效")
        expected_names = {MANIFEST_NAME}
        validated: list[dict[str, Any]] = []
        seen: set[str] = set()
        seen_casefold: set[str] = set()
        total = 0
        for item in files:
            if not isinstance(item, dict):
                raise DeliveryError("备份manifest含无效文件项")
            relative = _safe_relative(item.get("path"))
            name = relative.as_posix()
            if name in seen or name.casefold() in seen_casefold:
                raise DeliveryError(f"备份manifest存在重复路径：{name}")
            seen.add(name)
            seen_casefold.add(name.casefold())
            expected_names.add(name)
            if not _SHA256_RE.fullmatch(str(item.get("sha256", ""))):
                raise DeliveryError(f"备份manifest SHA256格式无效：{relative.name}")
            size = item.get("size_bytes")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise DeliveryError(f"备份manifest文件大小无效：{relative.name}")
            try:
                info = archive.getinfo(name)
            except KeyError as error:
                raise DeliveryError(f"备份包缺少文件：{relative.name}") from error
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if unix_mode & 0o170000 == 0o120000:
                raise DeliveryError("备份包不能包含符号链接")
            digest = hashlib.sha256()
            size_read = 0
            with archive.open(info, "r") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
                    size_read += len(block)
            total += size_read
            if total > 1024 ** 4:
                raise DeliveryError("备份包解压后超过1TiB限制")
            if size_read != size or digest.hexdigest() != item["sha256"]:
                raise DeliveryError(f"备份内容hash或大小不匹配：{relative.name}")
            validated.append({"path": name, "size_bytes": size_read, "sha256": digest.hexdigest()})
        if set(names) != expected_names:
            raise DeliveryError("备份包含manifest未登记的额外成员")
        return manifest, validated


def verify_backup_bundle(bundle: Path) -> dict[str, Any]:
    manifest, records = _read_backup_archive(Path(bundle))
    return {
        "bundle_sha256": _sha256_file(Path(bundle)),
        "verified_files": len(records),
        "verified_bytes": sum(item["size_bytes"] for item in records),
        "components": manifest.get("components", []),
    }


def restore_backup_bundle(bundle: Path, destination: Path) -> dict[str, Any]:
    """Restore verified members atomically into a destination that does not exist."""
    destination = Path(destination)
    if ".." in destination.parts:
        raise DeliveryError("恢复目标不能包含上级目录")
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise DeliveryError("恢复目标必须尚不存在；拒绝覆盖任何已有目录")
    cursor = destination.parent
    while cursor != cursor.parent:
        if cursor.is_symlink():
            raise DeliveryError("恢复目标父路径不能包含符号链接")
        cursor = cursor.parent
    parent = destination.parent.resolve(strict=True)
    if not parent.is_dir():
        raise DeliveryError("恢复目标父目录必须是已存在目录")
    manifest, records = _read_backup_archive(Path(bundle))
    stage = Path(tempfile.mkdtemp(prefix=".wenjin-restore-", dir=parent))
    rebased_versions = 0
    try:
        with zipfile.ZipFile(bundle, "r") as archive:
            for item in records:
                name = item["path"]
                relative = _safe_relative(name)
                target = stage.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size_written = 0
                with archive.open(name, "r") as source, target.open("xb") as output:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        output.write(block)
                        digest.update(block)
                        size_written += len(block)
                if size_written != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
                    raise DeliveryError(f"恢复期间备份内容发生变化：{relative.name}")
        rebased_versions = _rebase_managed_raw_roots(
            stage, destination, manifest.get("managed_raw_layout"),
            manifest.get("source_project_root"),
        )
        if destination.exists():
            raise DeliveryError("恢复目标在执行期间出现，拒绝覆盖")
        stage.replace(destination)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {
        "destination": str(destination),
        "restored_files": len(records),
        "rebased_managed_raw_versions": rebased_versions,
        "source_bundle_sha256": _sha256_file(Path(bundle)),
        "created_at_utc": manifest.get("created_at_utc"),
    }


def _managed_raw_layout(project_root: Path, manager_database: Path | None) -> str | None:
    """Bind managed absolute paths to one of the two deployed fixed layouts."""
    if manager_database is None or not Path(manager_database).is_file():
        return None
    connection = sqlite3.connect(f"file:{Path(manager_database).as_posix()}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(versions)")}
        if not {"raw_root", "file_id", "version"}.issubset(columns):
            raise DeliveryError("manager.sqlite缺少managed raw root字段")
        rows = connection.execute(
            "SELECT file_id,version,raw_root FROM versions WHERE raw_root IS NOT NULL"
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        return "raw_data"
    allowed = ("raw_data", "data/file-management/raw_data")
    for layout in allowed:
        expected = (project_root / Path(layout)).resolve()
        if all(
            isinstance(raw_root, str)
            and Path(raw_root).resolve() == expected / f"file-management-{file_id}" / str(version)
            for file_id, version, raw_root in rows
        ):
            return layout
    raise DeliveryError("managed raw根不属于已支持的原生或Docker固定布局")


def _rebase_managed_raw_roots(
    restored_tree: Path, final_root: Path, layout: str | None,
    source_project_root: str | None,
) -> int:
    """Rebase only manager rows using the product's fixed managed raw layout.

    The manager stores absolute paths in its version ledger and intentionally
    rejects a changed root. On restore we rewrite only paths shaped as
    raw_data/file-management-<file_id>/<version>, after archive hashes pass.
    """
    database_path = restored_tree / "data" / "file-management" / "manager.sqlite"
    if not database_path.is_file():
        return 0
    if layout not in {"raw_data", "data/file-management/raw_data"}:
        raise DeliveryError("备份manifest未绑定支持的managed raw布局，拒绝恢复")
    if not isinstance(source_project_root, str) or not Path(source_project_root).is_absolute():
        raise DeliveryError("备份manifest缺少原项目路径绑定，拒绝恢复")
    connection = sqlite3.connect(database_path, timeout=30)
    updates: list[tuple[str, str, str, str, str]] = []
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(versions)")}
        required = {"file_id", "version", "version_id", "raw_root", "source_path", "manifest_path", "bm25_path"}
        if not required.issubset(columns):
            raise DeliveryError("manager.sqlite的versions结构与当前managed root布局不兼容")
        rows = connection.execute(
            "SELECT file_id,version,version_id,raw_root,source_path,manifest_path,bm25_path "
            "FROM versions WHERE raw_root IS NOT NULL"
        ).fetchall()
        for file_id, version, version_id, raw_root, source_path, manifest_path, bm25_path in rows:
            if not all(isinstance(value, str) and value for value in
                       (file_id, version, version_id, raw_root, source_path, manifest_path, bm25_path)):
                raise DeliveryError("manager.sqlite含无效managed raw root绑定")
            layout_parts = list(PurePosixPath(layout).parts)
            raw_expected = [*layout_parts, f"file-management-{file_id}", version]
            expected_old_raw = Path(source_project_root).joinpath(*raw_expected).resolve()
            if Path(raw_root).resolve() != expected_old_raw:
                raise DeliveryError("managed raw路径不符合固定相对目录，拒绝恢复")
            source_name = re.split(r"[\\/]", source_path)[-1]
            if source_name in {"", ".", ".."} or any(char in '<>"|?*:' for char in source_name):
                raise DeliveryError("managed原文文件名无效，拒绝恢复")
            manifest_expected = ["data", "file-management", "versions", file_id, version_id, "source-manifest.json"]
            bm25_expected = ["data", "file-management", "indexes", file_id, f"{version_id}.sqlite"]
            manifest_parts = re.split(r"[\\/]+", manifest_path)[-len(manifest_expected):]
            bm25_parts = re.split(r"[\\/]+", bm25_path)[-len(bm25_expected):]
            if [part.casefold() for part in manifest_parts] != [part.casefold() for part in manifest_expected]:
                raise DeliveryError("managed manifest路径不符合固定目录，拒绝恢复")
            if [part.casefold() for part in bm25_parts] != [part.casefold() for part in bm25_expected]:
                raise DeliveryError("managed BM25路径不符合固定目录，拒绝恢复")
            new_raw = final_root.joinpath(*raw_expected).resolve()
            new_source = new_raw / source_name
            new_manifest = final_root.joinpath(*manifest_expected)
            new_bm25 = final_root.joinpath(*bm25_expected)
            for candidate in (new_raw, new_source, new_manifest, new_bm25):
                if not candidate.resolve().is_relative_to(final_root.resolve()):
                    raise DeliveryError("managed路径重定位越出恢复目录")
            staged_raw = restored_tree.joinpath(*raw_expected)
            staged_source = staged_raw / source_name
            staged_manifest = restored_tree.joinpath(*manifest_expected)
            staged_bm25 = restored_tree.joinpath(*bm25_expected)
            if not staged_raw.is_dir() or not staged_source.is_file() or not staged_manifest.is_file() or not staged_bm25.is_file():
                raise DeliveryError(f"恢复包缺少managed版本资产：{file_id}/{version}")
            updates.append((str(new_raw), str(new_source), str(new_manifest), str(new_bm25), str(version_id)))
        if updates:
            connection.execute("BEGIN IMMEDIATE")
            for new_root, new_source, new_manifest, new_bm25, version_id in updates:
                connection.execute(
                    "UPDATE versions SET raw_root=?,source_path=?,manifest_path=?,bm25_path=? WHERE version_id=?",
                    (new_root, new_source, str(new_manifest), str(new_bm25), version_id),
                )
            connection.commit()
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise DeliveryError("重定位managed路径后的manager.sqlite检查失败")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return len(updates)


def _parse_asset(value: str) -> dict[str, str]:
    match = _ASSET_RE.fullmatch(value)
    if not match:
        raise DeliveryError("asset格式必须是name@version=relative/path")
    relative = _safe_relative(match.group("path"))
    return {
        "name": match.group("name"),
        "version": match.group("version").strip(),
        "path": relative.as_posix(),
    }


def create_offline_manifest(
    root: Path,
    output: Path,
    assets: list[str],
    *,
    host_target: str,
    docker_desktop_version: str,
    docker_engine_version: str,
) -> dict[str, Any]:
    root = Path(root).resolve(strict=True)
    if host_target not in {"windows10", "windows11"}:
        raise DeliveryError("host_target只能是windows10或windows11")
    if not docker_desktop_version.strip() or not docker_engine_version.strip():
        raise DeliveryError("必须记录Docker Desktop与Engine版本")
    if not assets:
        raise DeliveryError("至少提供一个离线交付资产")
    entries = []
    names = set()
    for value in assets:
        item = _parse_asset(value)
        if item["name"] in names:
            raise DeliveryError(f"资产name重复：{item['name']}")
        names.add(item["name"])
        source = _ensure_no_symlink_ancestors(root, PurePosixPath(item["path"]))
        _ensure_regular_source(source)
        if not source.resolve().is_relative_to(root):
            raise DeliveryError("资产路径逃出manifest root")
        item["size_bytes"] = source.stat().st_size
        item["sha256"] = _sha256_file(source)
        entries.append(item)
    manifest = {
        "schema_version": OFFLINE_SCHEMA,
        "host_target": host_target,
        "docker_desktop_version": docker_desktop_version.strip(),
        "docker_engine_version": docker_engine_version.strip(),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "assets": entries,
        "integrity_scope": "sha256-accidental-corruption-detection; not a signature",
    }
    output = Path(output).absolute()
    if output.exists():
        raise FileExistsError(f"manifest输出已存在，拒绝覆盖：{output.name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"manifest": str(output), "assets": len(entries), "sha256": _sha256_file(output)}


def verify_offline_manifest(
    manifest_path: Path,
    root: Path,
    *,
    expected_versions: dict[str, str] | None = None,
    expected_host: str | None = None,
) -> dict[str, Any]:
    manifest_path = Path(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeliveryError("无法读取有效离线资产manifest") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != OFFLINE_SCHEMA:
        raise DeliveryError("不支持的离线资产manifest版本")
    host = manifest.get("host_target")
    if host not in {"windows10", "windows11"} or (expected_host and host != expected_host):
        raise DeliveryError("host_target与目标Windows版本不匹配")
    if not manifest.get("docker_desktop_version") or not manifest.get("docker_engine_version"):
        raise DeliveryError("manifest缺少Docker Desktop/Engine版本")
    root = Path(root).resolve(strict=True)
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise DeliveryError("manifest资产列表为空或无效")
    names: set[str] = set()
    checked = []
    for item in assets:
        if not isinstance(item, dict):
            raise DeliveryError("manifest含无效资产项")
        name, version = item.get("name"), item.get("version")
        if not isinstance(name, str) or not name or name in names:
            raise DeliveryError("manifest资产name为空或重复")
        if not isinstance(version, str) or not version.strip():
            raise DeliveryError(f"manifest资产缺少版本：{name}")
        names.add(name)
        if expected_versions and name in expected_versions and expected_versions[name] != version:
            raise DeliveryError(f"资产版本与要求不符：{name}")
        relative = _safe_relative(item.get("path"))
        source = _ensure_no_symlink_ancestors(root, relative)
        _ensure_regular_source(source)
        if not source.resolve().is_relative_to(root):
            raise DeliveryError(f"资产路径逃出root：{name}")
        expected_hash = item.get("sha256")
        expected_size = item.get("size_bytes")
        if not isinstance(expected_hash, str) or not _SHA256_RE.fullmatch(expected_hash):
            raise DeliveryError(f"资产SHA256格式无效：{name}")
        if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
            raise DeliveryError(f"资产大小无效：{name}")
        actual_hash = _sha256_file(source)
        actual_size = source.stat().st_size
        if actual_hash != expected_hash or actual_size != expected_size:
            raise DeliveryError(f"资产大小或SHA256不匹配：{name}")
        checked.append({"name": name, "version": version, "sha256": actual_hash, "size_bytes": actual_size})
    return {
        "manifest_sha256": _sha256_file(manifest_path),
        "host_target": host,
        "verified_assets": len(checked),
        "assets": checked,
    }


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _redact_logs(value: str, *, max_chars: int = 12000) -> str:
    safe_lines = []
    for line in value.splitlines()[-200:]:
        safe_lines.append(_LOG_SECRET_RE.sub(r"\1[REDACTED]", line[:1000]))
    return "\n".join(safe_lines)[-max_chars:]


def _redact_known_secrets(value: str, environment: dict[str, str]) -> str:
    """Redact values of secret-named inherited variables without recording them."""
    secret_values = sorted(
        {
            item for key, item in environment.items()
            if re.search(r"(?i)(token|password|secret|api.?key|credential)", key)
            and isinstance(item, str) and len(item) >= 4
        },
        key=len,
        reverse=True,
    )
    for secret in secret_values:
        value = value.replace(secret, "[REDACTED]")
    return _redact_logs(value)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _run_logged_child(
    command: list[str],
    *,
    cwd: Path,
    output_dir: Path,
    service: str,
    config_identity: dict[str, str],
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one process, write redacted output, and seal start/end evidence.

    `command` is private to this module and deliberately never serialized. Public
    CLI commands below select only the management or legacy MCP entry points.
    """
    output_dir = Path(output_dir).absolute()
    if output_dir.is_symlink():
        raise DeliveryError("启动证据目录不能是符号链接")
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        raise DeliveryError("启动证据目录必须为空；拒绝覆盖旧日志")
    output_dir.mkdir(parents=True, exist_ok=True)
    cwd = Path(cwd).resolve(strict=True)
    env = dict(os.environ if environment is None else environment)
    run_id = uuid4().hex
    record_path = output_dir / "run-record.json"
    log_path = output_dir / "service-redacted.log"
    started_at = datetime.now(timezone.utc).isoformat()
    record: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "service": service,
        "config_identity": config_identity,
        "started_at_utc": started_at,
        "finished_at_utc": None,
        "pid": None,
        "exit_code": None,
        "result": "starting",
        "command_recorded": False,
        "environment_recorded": False,
    }
    _write_json_atomic(record_path, record)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
            start_new_session=(os.name != "nt"),
        )
    except OSError as error:
        record.update({
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "result": "launch_failed",
            "launch_error": type(error).__name__,
        })
        _write_json_atomic(record_path, record)
        return record
    record["pid"] = process.pid
    record["result"] = "running"
    _write_json_atomic(record_path, record)
    log_error: list[str] = []

    def drain() -> None:
        try:
            with log_path.open("x", encoding="utf-8", newline="\n") as log:
                assert process.stdout is not None
                for line in process.stdout:
                    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                    log.write(f"{stamp} { _redact_known_secrets(line.rstrip(), env) }\n")
                    log.flush()
        except Exception as error:  # diagnostic capture failure must be recorded, not hide exit
            log_error.append(type(error).__name__)

    reader = threading.Thread(target=drain, name=f"delivery-log-{run_id[:8]}", daemon=True)
    reader.start()
    requested_signal = None
    try:
        while process.poll() is None:
            time.sleep(0.2)
    except KeyboardInterrupt:
        requested_signal = "interrupt"
        if os.name == "nt":
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            except OSError:
                process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    finally:
        exit_code = process.wait()
        reader.join(timeout=5)
    record.update({
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "exit_code": exit_code,
        "result": "exited" if exit_code == 0 else "failed",
        "stop_requested": requested_signal,
    })
    if log_error:
        record["log_capture_error"] = log_error[0]
    _write_json_atomic(record_path, record)
    return record


def launch_managed_service(
    service: str,
    python: Path,
    project_root: Path,
    output_dir: Path,
    *,
    host: str | None = None,
    port: int | None = None,
) -> dict[str, Any]:
    """Launch only the project's management HTTP or legacy stateful MCP entry."""
    root = Path(project_root).resolve(strict=True)
    interpreter = Path(python).resolve(strict=True)
    if not interpreter.is_file() or not root.is_dir():
        raise DeliveryError("Python解释器或项目root不存在")
    if service == "management":
        host = host or "127.0.0.1"
        port = 18777 if port is None else port
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise DeliveryError("管理服务launcher只允许loopback绑定")
        if not 1 <= port <= 65535:
            raise DeliveryError("管理端口无效")
        # Source is a fixed entrypoint; no token, secret, or environment is passed in argv.
        configured_identity = os.environ.get("WENJIN_IDENTITY_DB")
        identity_db = Path(configured_identity).expanduser().resolve() if configured_identity else None
        bootstrap = (
            "import os,sys,uvicorn; from pathlib import Path; "
            "from wenjin.management_http.app import create_app; "
            "identity_db=Path(sys.argv[4]) if sys.argv[4] else None; "
            "uvicorn.run(create_app(Path(sys.argv[1]), admin_token=os.environ.get('WENJIN_FILE_ADMIN_TOKEN'), "
            "identity_db=identity_db), "
            "host=sys.argv[2], port=int(sys.argv[3]), log_config=None)"
        )
        command = [str(interpreter), "-c", bootstrap, str(root), host, str(port), str(identity_db or "")]
        config = {"host": host, "port": str(port), "route": "/admin/", "project_root_name": root.name,
                  "identity_database": identity_db.name if identity_db else "not-configured"}
    elif service == "legacy-mcp":
        host = host or "127.0.0.1"
        port = 18765 if port is None else port
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise DeliveryError("MCP launcher只允许loopback绑定")
        if not 1 <= port <= 65535:
            raise DeliveryError("MCP端口无效")
        command = [str(interpreter), "-m", "wenjin.mcp_server", "--host", host,
                   "--port", str(port), "--path", "/mcp", "--project-root", str(root)]
        config = {"host": host, "port": str(port), "route": "/mcp", "protocol": "stateful-http",
                  "project_root_name": root.name,
                  "identity_database": "resolved by service configuration; value omitted"}
    else:
        raise DeliveryError("service只能是management或legacy-mcp")
    return _run_logged_child(command, cwd=root, output_dir=Path(output_dir),
                             service=service, config_identity=config)


def collect_owned_docker_diagnostics(
    container_id: str,
    run_id: str,
    output: Path,
    *,
    include_logs: bool = False,
    docker_executable: str = "docker",
) -> dict[str, Any]:
    """Collect a narrow inspect record only when the caller proves the run label."""
    if not container_id.strip() or not run_id.strip():
        raise DeliveryError("必须提供container id与启动记录中的run id")
    inspect = _run([docker_executable, "inspect", container_id])
    if inspect.returncode != 0:
        raise DeliveryError("docker inspect失败；不生成诊断包")
    try:
        raw = json.loads(inspect.stdout)
        item = raw[0]
    except (json.JSONDecodeError, IndexError, TypeError, KeyError) as error:
        raise DeliveryError("docker inspect返回无效数据") from error
    labels = ((item.get("Config") or {}).get("Labels") or {})
    if labels.get("com.wenjin.delivery.run_id") != run_id:
        raise DeliveryError("容器没有匹配本次启动记录的run-id标签；拒绝读取其日志")
    state = item.get("State") or {}
    config = item.get("Config") or {}
    record: dict[str, Any] = {
        "schema_version": 1,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "container": {
            "id": item.get("Id"),
            "name": item.get("Name"),
            "image": config.get("Image"),
            "run_label": run_id,
            "status": state.get("Status"),
            "exit_code": state.get("ExitCode"),
            "oom_killed": state.get("OOMKilled"),
            "error": state.get("Error"),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "restart_count": item.get("RestartCount"),
            "restart_policy": ((item.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name"),
        },
    }
    version = _run([docker_executable, "version", "--format", "{{.Server.Version}}"])
    record["docker_engine_version"] = version.stdout.strip() if version.returncode == 0 else None
    if include_logs:
        logs = _run([docker_executable, "logs", "--timestamps", "--tail", "200", container_id])
        if logs.returncode != 0:
            record["logs_error"] = "docker logs读取失败"
        else:
            record["logs_redacted"] = _redact_logs(logs.stdout + logs.stderr)
    output = Path(output).absolute()
    if output.exists():
        raise FileExistsError(f"诊断输出已存在，拒绝覆盖：{output.name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"diagnostic": str(output), "sha256": _sha256_file(output), "exit_code": state.get("ExitCode")}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Windows离线交付资产校验与隔离恢复工具")
    commands = parser.add_subparsers(dest="command", required=True)
    make_manifest = commands.add_parser("manifest", help="从显式离线资产生成版本/hash清单")
    make_manifest.add_argument("--root", type=Path, required=True)
    make_manifest.add_argument("--output", type=Path, required=True)
    make_manifest.add_argument("--asset", action="append", required=True, help="name@version=相对路径")
    make_manifest.add_argument("--host-target", choices=("windows10", "windows11"), required=True)
    make_manifest.add_argument("--docker-desktop-version", required=True)
    make_manifest.add_argument("--docker-engine-version", required=True)
    check_manifest = commands.add_parser("verify-manifest", help="校验manifest路径、版本、大小与SHA256")
    check_manifest.add_argument("--manifest", type=Path, required=True)
    check_manifest.add_argument("--root", type=Path, required=True)
    check_manifest.add_argument("--host-target", choices=("windows10", "windows11"))
    check_manifest.add_argument("--expect-version", action="append", default=[], help="name=version")
    backup = commands.add_parser("backup", help="对停写项目的显式组件创建hash归档")
    backup.add_argument("--root", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--include", action="append", required=True)
    verify = commands.add_parser("verify-backup", help="校验备份包内容hash")
    verify.add_argument("--bundle", type=Path, required=True)
    restore = commands.add_parser("restore", help="恢复到全新目录；拒绝覆盖")
    restore.add_argument("--bundle", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    diagnose = commands.add_parser("diagnose-container", help="读取匹配run-id容器的脱敏退出证据")
    diagnose.add_argument("--container-id", required=True)
    diagnose.add_argument("--run-id", required=True)
    diagnose.add_argument("--output", type=Path, required=True)
    diagnose.add_argument("--include-logs", action="store_true", help="附加最多200行有限脱敏日志")
    launch = commands.add_parser("launch-service", help="受限启动管理HTTP或旧版MCP并记录结束状态")
    launch.add_argument("--service", choices=("management", "legacy-mcp"), required=True)
    launch.add_argument("--python", type=Path, required=True)
    launch.add_argument("--project-root", type=Path, required=True)
    launch.add_argument("--output-dir", type=Path, required=True)
    launch.add_argument("--host", choices=("127.0.0.1", "localhost", "::1"))
    launch.add_argument("--port", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "manifest":
        result = create_offline_manifest(
            args.root, args.output, args.asset, host_target=args.host_target,
            docker_desktop_version=args.docker_desktop_version,
            docker_engine_version=args.docker_engine_version,
        )
    elif args.command == "verify-manifest":
        expected = {}
        for item in args.expect_version:
            if "=" not in item:
                raise DeliveryError("--expect-version格式必须为name=version")
            name, version = item.split("=", 1)
            expected[name] = version
        result = verify_offline_manifest(args.manifest, args.root, expected_versions=expected,
                                         expected_host=args.host_target)
    elif args.command == "backup":
        result = create_backup_bundle(args.root, args.include, args.output)
    elif args.command == "verify-backup":
        result = verify_backup_bundle(args.bundle)
    elif args.command == "restore":
        result = restore_backup_bundle(args.bundle, args.destination)
    elif args.command == "diagnose-container":
        result = collect_owned_docker_diagnostics(
            args.container_id, args.run_id, args.output, include_logs=args.include_logs,
        )
    else:
        result = launch_managed_service(args.service, args.python, args.project_root,
                                        args.output_dir, host=args.host, port=args.port)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
