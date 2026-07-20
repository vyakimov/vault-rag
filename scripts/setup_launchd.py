#!/usr/bin/env python3
"""Install Vault Spider's periodic sync as a macOS per-user LaunchAgent.

Dry-run is the default. Use ``--apply`` to install or update the agent and
start it immediately. Lint is optional and disabled by default. Enrichment is
intentionally not scheduled because it requires a specific note and produces a
plan that should be reviewed before any mutation is applied.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, NoReturn, Optional

LABEL = "ai.vault-spider.sync"
DEFAULT_INTERVAL_MINUTES = 60
MIN_INTERVAL_MINUTES = 5


class SetupError(RuntimeError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise SetupError(message)


def _launchctl(arguments: List[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["launchctl", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and process.returncode != 0:
        detail = (process.stderr or process.stdout).strip() or f"exit {process.returncode}"
        raise SetupError(f"launchctl {' '.join(arguments)} failed: {detail}")
    return process


def _service_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def _loaded() -> bool:
    return _launchctl(["print", _service_target()], check=False).returncode == 0


def _paths(home: Path) -> Dict[str, Path]:
    return {
        "agent": home / "Library" / "LaunchAgents" / f"{LABEL}.plist",
        "logs": home / "Library" / "Logs" / "VaultSpider",
        "stdout": home / "Library" / "Logs" / "VaultSpider" / "sync.stdout.log",
        "stderr": home / "Library" / "Logs" / "VaultSpider" / "sync.stderr.log",
        "state": home / "Library" / "Caches" / "VaultSpider",
    }


def build_plist(
    repo: Path,
    uv_binary: Path,
    home: Path,
    interval_minutes: int,
    with_lint: bool,
) -> Dict[str, Any]:
    runner = repo / "scripts" / "periodic_maintenance.py"
    paths = _paths(home)
    launch_path = os.pathsep.join(
        dict.fromkeys(
            [
                str(uv_binary.parent),
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            ]
        )
    )
    return {
        "Label": LABEL,
        # Launch uv directly rather than /usr/bin/python3. On macOS, a
        # background system Python can be denied access to repositories and
        # vaults under ~/Documents even when uv is already permitted.
        "ProgramArguments": [
            str(uv_binary),
            "run",
            "--project",
            str(repo),
            "python",
            str(runner),
        ],
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {
            "PATH": launch_path,
            "PYTHONUNBUFFERED": "1",
            "VAULT_SPIDER_RUN_LINT": "1" if with_lint else "0",
            "VAULT_SPIDER_MAINTENANCE_STATE": str(paths["state"]),
        },
        "RunAtLoad": True,
        "StartInterval": interval_minutes * 60,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
        "ThrottleInterval": 60,
        "StandardOutPath": str(paths["stdout"]),
        "StandardErrorPath": str(paths["stderr"]),
    }


def build_plan(
    repo: Path,
    uv_binary: Path,
    home: Path,
    interval_minutes: int,
    with_lint: bool,
    uninstall: bool,
) -> Dict[str, Any]:
    paths = _paths(home)
    plan: Dict[str, Any] = {
        "label": LABEL,
        "service_target": _service_target(),
        "agent_path": str(paths["agent"]),
        "loaded": _loaded(),
        "installed": paths["agent"].exists(),
        "operation": "uninstall" if uninstall else "install-or-update",
        "interval_minutes": interval_minutes,
        "run_at_load": True,
        "lint_enabled": with_lint,
        "enrich_enabled": False,
        "stdout_log": str(paths["stdout"]),
        "stderr_log": str(paths["stderr"]),
        "state_dir": str(paths["state"]),
        "uv_binary": str(uv_binary),
        "macos_privacy": (
            "If macOS denies a repository or vault under ~/Documents, allow the listed uv "
            "binary Documents Folder access in Privacy & Security."
        ),
    }
    if not uninstall:
        plan["plist"] = build_plist(repo, uv_binary, home, interval_minutes, with_lint)
    return plan


def _validated_plist_bytes(plist: Dict[str, Any]) -> bytes:
    payload = plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)

    # `plutil` is the authoritative validator (it understands launchd-specific
    # semantics), but it only exists on macOS. Off Darwin — notably Linux CI,
    # which never installs this LaunchAgent — fall back to plistlib's own
    # parser so the XML shape is still checked instead of skipping validation.
    plutil = shutil.which("plutil")
    if plutil is None:
        try:
            plistlib.loads(payload)
        except plistlib.InvalidFileException as exc:
            raise SetupError(f"generated plist is invalid: {exc}") from exc
        return payload

    with tempfile.NamedTemporaryFile(suffix=".plist", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
    try:
        process = subprocess.run(
            [plutil, "-lint", str(temporary)],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode != 0:
            detail = (process.stderr or process.stdout).strip()
            raise SetupError(f"generated plist is invalid: {detail}")
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
        temporary.chmod(0o644)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def apply_install(plist: Dict[str, Any], home: Path) -> Dict[str, Any]:
    paths = _paths(home)
    payload = _validated_plist_bytes(plist)
    paths["logs"].mkdir(parents=True, exist_ok=True)
    paths["logs"].chmod(0o700)
    paths["state"].mkdir(parents=True, exist_ok=True)
    paths["state"].chmod(0o700)

    was_loaded = _loaded()
    previous = paths["agent"].read_bytes() if paths["agent"].exists() else None
    if was_loaded:
        _launchctl(["bootout", _service_target()])
    try:
        _atomic_write(paths["agent"], payload)
        _launchctl(["enable", _service_target()])
        # RunAtLoad starts the job as part of bootstrap. A subsequent
        # ``kickstart -k`` would terminate that first run and trigger launchd's
        # throttle window before any sync output is written.
        _launchctl(["bootstrap", f"gui/{os.getuid()}", str(paths["agent"])])
    except BaseException:
        if _loaded():
            _launchctl(["bootout", _service_target()], check=False)
        if previous is None:
            paths["agent"].unlink(missing_ok=True)
        else:
            _atomic_write(paths["agent"], previous)
            if was_loaded:
                _launchctl(
                    ["bootstrap", f"gui/{os.getuid()}", str(paths["agent"])],
                    check=False,
                )
        raise
    return {"applied": True, "started": True, "agent_path": str(paths["agent"])}


def apply_uninstall(home: Path) -> Dict[str, Any]:
    paths = _paths(home)
    if _loaded():
        _launchctl(["bootout", _service_target()])
    removed = paths["agent"].exists()
    paths["agent"].unlink(missing_ok=True)
    return {
        "applied": True,
        "removed": removed,
        "preserved_logs": str(paths["logs"]),
        "preserved_state": str(paths["state"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the plan (default: dry-run)")
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=DEFAULT_INTERVAL_MINUTES,
        help=f"Sync interval (default: {DEFAULT_INTERVAL_MINUTES})",
    )
    parser.add_argument(
        "--with-lint",
        action="store_true",
        help="Also run read-only lint after each successful sync",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Unload and remove the LaunchAgent; logs and state are preserved",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.interval_minutes < MIN_INTERVAL_MINUTES:
            raise SetupError(
                f"--interval-minutes must be at least {MIN_INTERVAL_MINUTES}"
            )
        repo = Path(__file__).resolve().parents[1]
        runner = repo / "scripts" / "periodic_maintenance.py"
        wrapper = repo / "bin" / "vault-spider"
        if not runner.is_file() or not wrapper.is_file():
            raise SetupError("Vault Spider runner or wrapper is missing")
        uv = shutil.which("uv")
        if not uv:
            raise SetupError("uv is not available on PATH")
        uv_binary = Path(uv).resolve()
        home = Path.home().resolve()
        plan = build_plan(
            repo,
            uv_binary,
            home,
            args.interval_minutes,
            args.with_lint,
            args.uninstall,
        )
        result: Dict[str, Any] = {"dry_run": not args.apply, "plan": plan}
        if args.apply:
            if args.uninstall:
                result.update(apply_uninstall(home))
            else:
                result.update(apply_install(plan["plist"], home))
        print(json.dumps({"ok": True, "action": "setup-launchd", "result": result}))
        return 0
    except (OSError, SetupError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "action": "setup-launchd",
                    "error": {"type": "setup_error", "message": str(exc)},
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
