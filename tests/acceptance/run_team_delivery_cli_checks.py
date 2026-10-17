"""Run bounded Claude Code/OpenCode MCP discovery checks in isolated temp homes."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _isolated_environment(home: Path) -> dict[str, str]:
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
    environment = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    paths = {
        "HOME": home / "home",
        "USERPROFILE": home / "userprofile",
        "APPDATA": home / "appdata" / "roaming",
        "LOCALAPPDATA": home / "appdata" / "local",
        "XDG_CONFIG_HOME": home / "xdg" / "config",
        "XDG_DATA_HOME": home / "xdg" / "data",
        "XDG_CACHE_HOME": home / "xdg" / "cache",
        "XDG_STATE_HOME": home / "xdg" / "state",
    }
    for key, path in paths.items():
        path.mkdir(parents=True, exist_ok=True)
        environment[key] = str(path)
    return environment


def _run(label: str, command: list[str], *, cwd: Path, env: dict[str, str], token: str) -> int:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    stdout = result.stdout.replace(token, "<synthetic-bearer-redacted>")
    stderr = result.stderr.replace(token, "<synthetic-bearer-redacted>")
    print(f"[{label}] exit={result.returncode}")
    if stdout.strip():
        print(stdout[-6000:])
    if stderr.strip():
        print("stderr:")
        print(stderr[-6000:])
    if label.startswith("claude") and (
        "Pending approval" in stdout or "No MCP servers configured" in stdout
    ):
        print(f"[{label}] outcome=partial; command exit alone is not connection evidence")
        return max(result.returncode, 2)
    return result.returncode


def _windows_command(executable: str, arguments: list[str]) -> list[str]:
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        command_line = subprocess.list2cmdline([executable, *arguments])
        return [env_value("COMSPEC", "cmd.exe"), "/d", "/c", command_line]
    return [executable, *arguments]


def env_value(name: str, fallback: str) -> str:
    return os.environ.get(name) or fallback


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("descriptor", type=Path)
    parser.add_argument("--claude-only", action="store_true")
    args = parser.parse_args()
    descriptor = json.loads(args.descriptor.read_text(encoding="utf-8"))
    endpoint = descriptor["endpoint"]
    token = descriptor["bearer"]
    if descriptor.get("fixture_classification") != "synthetic MCP/tool evidence only":
        raise SystemExit("Refusing a non-synthetic MCP fixture descriptor")

    with tempfile.TemporaryDirectory(prefix="wenjin-team-delivery-cli-") as temporary:
        base = Path(temporary)
        work = base / "cwd"
        work.mkdir()
        claude_config = work / ".mcp.json"
        claude_config.write_text(
            json.dumps({"mcpServers": {
                "synthetic-wenjin": {
                    "type": "http",
                    "url": endpoint,
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }}, separators=(",", ":")),
            encoding="utf-8",
        )
        claude_settings = work / ".claude" / "settings.local.json"
        claude_settings.parent.mkdir(parents=True, exist_ok=True)
        claude_settings.write_text(
            json.dumps({"enableAllProjectMcpServers": True}),
            encoding="utf-8",
        )
        claude = shutil.which("claude")
        if claude is None:
            print("[claude] executable not found; not run")
            claude_code = 127
        else:
            claude_home = base / "claude-home"
            claude_env = _isolated_environment(claude_home)
            claude_code = _run(
                "claude mcp list",
                _windows_command(claude, [
                    "--bare", "--setting-sources", "project,local",
                    "--no-session-persistence", "mcp", "list",
                ]),
                cwd=work,
                env=claude_env,
                token=token,
            )

        if args.claude_only:
            opencode_code = 0
        else:
            opencode = shutil.which("opencode")
            if opencode is None:
                # Parent may provide an isolated npm shim rather than a PATH install.
                configured_opencode = os.environ.get("WENJIN_OPENCODE_EXECUTABLE")
                opencode = configured_opencode if configured_opencode and Path(configured_opencode).is_file() else None
            if opencode is None:
                print("[opencode] executable not found; not run")
                opencode_code = 127
            else:
                open_home = base / "opencode-home"
                open_env = _isolated_environment(open_home)
                open_config = {
                    "mcp": {
                        "synthetic-wenjin": {
                            "type": "remote",
                            "url": endpoint,
                            "headers": {"Authorization": f"Bearer {token}"},
                            "oauth": False,
                        }
                    }
                }
                open_env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
                    open_config, separators=(",", ":")
                )
                open_env["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
                open_env["OPENCODE_DISABLE_MODELS_FETCH"] = "true"
                opencode_code = _run(
                    "opencode mcp list",
                    _windows_command(opencode, ["mcp", "list", "--pure"]),
                    cwd=work,
                    env=open_env,
                    token=token,
                )
    return int(claude_code != 0 or opencode_code != 0)


if __name__ == "__main__":
    raise SystemExit(main())
