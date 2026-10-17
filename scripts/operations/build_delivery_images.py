"""Stage and optionally build source-bound images from inventoried local bases.

Building is gated by an explicit source hash and --build. Docker is never
invoked while this script only stages the source context.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

BASES = {
    "backend": (
        "wenjin-backend:2026-10-14",
        "sha256:6941765e2224efa160aa01e5a2518563d6c2bd9f3b93a2940ce33a90215d37cd",
        "wenjin-backend",
    ),
    "model-node": (
        "wenjin-model-node:dual-smoke-20261008",
        "sha256:c9d4e5172eca901fcec1d3389c8c119183eacd8b8788e4a3fbb8838dc2fdca3e",
        "wenjin-model-node",
    ),
    "mcp-stateless": (
        "wenjin-mcp-stateless:2026-10-14-libxml-2.11.9",
        "sha256:74ca2f90a09b0aa492d0971d72ef6659792d372121f08dda0edeea6968ff7a03",
        "wenjin-mcp-stateless",
    ),
}


def _smoke_image(tag: str, *, runtime_sha256: str, cli_module: str | None) -> None:
    check = (
        "import hashlib; from pathlib import Path; "
        "from wenjin.mcp.runtime import ROOT; "
        "p=Path('/app/src/wenjin/mcp/runtime.py').resolve(); "
        "assert ROOT.resolve() == Path('/app').resolve(), ROOT; "
        "assert p.is_file() and p == Path('/app/src/wenjin/mcp/runtime.py'), p; "
        f"assert hashlib.sha256(p.read_bytes()).hexdigest() == '{runtime_sha256}', p"
    )
    subprocess.run(
        ["docker", "run", "--rm", "--network=none", "--entrypoint", "python", tag,
         "-c", check],
        check=True, capture_output=True, text=True,
    )
    if cli_module:
        subprocess.run(
            ["docker", "run", "--rm", "--network=none", "--entrypoint", "python", tag,
             "-m", cli_module, "--help"],
            check=True, capture_output=True, text=True,
        )


def source_rows(root: Path) -> list[dict[str, object]]:
    rows = []
    for folder in ("src", "scripts"):
        base = root / folder
        if not base.is_dir():
            raise ValueError(f"required source directory missing: {folder}")
        for source in sorted(base.rglob("*")):
            if source.is_symlink():
                raise ValueError(f"source contains symbolic link: {source.relative_to(root)}")
            if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc":
                continue
            name = source.name.casefold()
            if name.startswith(".env") or any(
                marker in name for marker in ("credential", "secret", "private-key")
            ):
                raise ValueError(f"sensitive-looking source refused: {source.relative_to(root)}")
            payload = source.read_bytes()
            rows.append({
                "path": source.relative_to(root).as_posix(),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
    return rows


def prepare(root: Path, output: Path, *, expected_source_sha256: str | None, build: bool) -> dict:
    root = root.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(root):
        raise ValueError("output must not exist and must be outside source checkout")
    rows = source_rows(root)
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    source_sha = hashlib.sha256(canonical).hexdigest()
    if expected_source_sha256 and expected_source_sha256.lower() != source_sha:
        raise ValueError("source SHA256 does not match supplied freeze hash")
    if build and not expected_source_sha256:
        raise ValueError("--build requires --expect-source-sha256 as a freeze gate")

    output.mkdir(parents=True)
    try:
        for row in rows:
            source = root / Path(str(row["path"]))
            target = output / Path(str(row["path"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        images = {}
        runtime_sha256 = next(
            str(row["sha256"]) for row in rows if row["path"] == "src/wenjin/mcp/runtime.py"
        )
        for role, (base, expected_id, repository) in BASES.items():
            tag = f"{repository}:delivery-{source_sha[:12]}"
            dockerfile = output / f"Dockerfile.{role}"
            dockerfile.write_text(
                f"FROM {base}\n"
                f'LABEL com.wenjin.delivery.role="{role}"\n'
                f'LABEL com.wenjin.delivery.source_sha256="{source_sha}"\n'
                "COPY src/ /app/src/\n"
                "COPY scripts/ /app/scripts/\n"
                "ENV PYTHONPATH=/app/src\n",
                encoding="utf-8",
            )
            images[role] = {
                "base_image": base,
                "expected_base_image_id": expected_id,
                "tag": tag,
                "dockerfile": dockerfile.name,
                "source_sha256": source_sha,
                "build": "not run" if not build else "pending",
            }
            if build:
                actual = subprocess.run(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", base],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
                if actual != expected_id:
                    raise ValueError(f"cached base image ID differs from inventory: {role}")
                subprocess.run(
                    ["docker", "build", "--network=none", "--file", dockerfile.name,
                     "--tag", tag, "."], cwd=output, check=True,
                )
                image_id = subprocess.run(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
                cli_module = (
                    "wenjin.file_management" if role == "backend"
                    else "wenjin.mcp.stateless_server" if role == "mcp-stateless"
                    else None
                )
                _smoke_image(tag, runtime_sha256=runtime_sha256, cli_module=cli_module)
                images[role]["image_id"] = image_id
                images[role]["runtime_root"] = "/app"
                images[role]["runtime_module_sha256"] = runtime_sha256
                images[role]["cli_help_checked"] = cli_module is not None
                images[role]["build"] = "built offline; source-bound runtime import and CLI --help passed"
        manifest = {
            "schema_version": 1,
            "source_sha256": source_sha,
            "source_files": rows,
            "images": images,
            "runtime_validation": "not run" if not build else "network-none import/root/hash and CLI --help passed",
            "offline_boot_validation": "not established by image construction",
        }
        (output / "delivery-images.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return manifest
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect-source-sha256")
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
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
        print(f"delivery image preparation failed: {type(error).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
