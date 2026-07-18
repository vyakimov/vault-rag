#!/usr/bin/env python3
"""Install and configure the Obsidian-side requirements for Vault Spider.

Dry-run is the default. With ``--apply`` the script uses the official Obsidian
CLI to install/enable the one recommended community plugin, then merges only
Vault Spider's required keys into the vault's existing JSON settings.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from vault_spider import settings
from vault_spider.obsidian import registry

PLUGIN_ID = "update-time-on-edit"
PLUGIN_REPO = "beaussan/update-time-on-edit-obsidian"
PLUGIN_SETTINGS: dict[str, Any] = {
    "dateFormat": "yyyy-MM-dd'T'HH:mm:ss",
    "enableCreateTime": False,
    "headerUpdated": "updated",
    "headerCreated": "created",
}
PLUGIN_FRESH_DEFAULTS: dict[str, Any] = {
    "minMinutesBetweenSaves": 4,
    "ignoreGlobalFolder": [],
    "ignoreCreatedFolder": [],
    "enableExperimentalHash": False,
    "fileHashMap": {},
}
PROPERTY_TYPES = {"created": "datetime", "updated": "datetime"}
APP_SETTINGS = {"alwaysUpdateLinks": True}
CORE_PLUGINS = {"properties": True}
CLI_CANDIDATES = (
    "/usr/local/bin/obsidian",
    "/Applications/Obsidian.app/Contents/MacOS/obsidian-cli",
    "/Applications/Obsidian.app/Contents/MacOS/Obsidian",
)


class SetupError(RuntimeError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise SetupError(message)


def _load_json(path: Path, default: Any, expected_type: type) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(payload, expected_type):
        raise SetupError(f"{path} must contain a JSON {expected_type.__name__}")
    return payload


def _changed_keys(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return sorted(key for key in after if before.get(key) != after.get(key))


def _documents(root: Path, min_update_minutes: int | None = None) -> dict[str, Any]:
    obsidian = root / ".obsidian"
    plugin_dir = obsidian / "plugins" / PLUGIN_ID
    data_path = plugin_dir / "data.json"
    types_path = obsidian / "types.json"
    app_path = obsidian / "app.json"
    core_path = obsidian / "core-plugins.json"
    community_path = obsidian / "community-plugins.json"

    plugin_before = _load_json(data_path, {}, dict)
    plugin_after = copy.deepcopy(plugin_before)
    for key, value in PLUGIN_FRESH_DEFAULTS.items():
        plugin_after.setdefault(key, copy.deepcopy(value))
    plugin_after.update(PLUGIN_SETTINGS)
    if min_update_minutes is not None:
        plugin_after["minMinutesBetweenSaves"] = min_update_minutes

    types_before = _load_json(types_path, {"types": {}}, dict)
    types_after = copy.deepcopy(types_before)
    type_map = types_after.setdefault("types", {})
    if not isinstance(type_map, dict):
        raise SetupError(f"{types_path}: 'types' must be a JSON object")
    type_map.update(PROPERTY_TYPES)

    app_before = _load_json(app_path, {}, dict)
    app_after = copy.deepcopy(app_before)
    app_after.update(APP_SETTINGS)

    core_before = _load_json(core_path, {}, dict)
    core_after = copy.deepcopy(core_before)
    core_after.update(CORE_PLUGINS)

    community_before = _load_json(community_path, [], list)
    if not all(isinstance(item, str) for item in community_before):
        raise SetupError(f"{community_path} must contain a JSON list of plugin ids")
    community_after = list(community_before)
    if PLUGIN_ID not in community_after:
        community_after.append(PLUGIN_ID)

    return {
        "plugin_dir": plugin_dir,
        "plugin_installed": (plugin_dir / "manifest.json").is_file()
        and (plugin_dir / "main.js").is_file(),
        "plugin_enabled": PLUGIN_ID in community_before,
        "files": {
            "plugin_settings": (data_path, plugin_before, plugin_after),
            "property_types": (types_path, types_before, types_after),
            "app_settings": (app_path, app_before, app_after),
            "core_plugins": (core_path, core_before, core_after),
            "community_plugins": (community_path, community_before, community_after),
        },
    }


def build_plan(root: Path, min_update_minutes: int | None = None) -> dict[str, Any]:
    state = _documents(root, min_update_minutes)
    changes: dict[str, Any] = {}
    for label, (path, before, after) in state["files"].items():
        if isinstance(before, dict) and isinstance(after, dict):
            changed = _changed_keys(before, after)
            if label == "property_types":
                changed = _changed_keys(before.get("types", {}), after.get("types", {}))
        else:
            changed = [PLUGIN_ID] if before != after else []
        changes[label] = {"path": str(path), "changed_keys": changed, "changed": before != after}

    actions: list[str] = []
    if not state["plugin_installed"]:
        actions.append(f"install community plugin {PLUGIN_ID} from {PLUGIN_REPO}")
    if not state["plugin_enabled"]:
        actions.append(f"enable community plugin {PLUGIN_ID}")
    for label, details in changes.items():
        if details["changed"]:
            actions.append(f"merge {label}: {', '.join(details['changed_keys'])}")
    return {
        "root": str(root),
        "plugin": {
            "id": PLUGIN_ID,
            "repo": PLUGIN_REPO,
            "installed": state["plugin_installed"],
            "enabled": state["plugin_enabled"],
        },
        "changes": changes,
        "actions": actions,
    }


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
        if existing_mode is not None:
            os.chmod(path, existing_mode)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _backup(root: Path, paths: list[Path]) -> Path | None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = root / ".obsidian" / ".vault-spider-backups" / stamp
    suffix = 1
    while backup.exists():
        backup = backup.with_name(f"{stamp}-{suffix}")
        suffix += 1
    for path in existing:
        destination = backup / path.relative_to(root / ".obsidian")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    return backup


def _resolve_binary(explicit: str | None) -> str:
    configured = explicit or settings.obsidian_binary()
    if configured:
        candidate = shutil.which(configured) if "/" not in configured else configured
        if candidate and Path(candidate).exists():
            return str(candidate)
        raise SetupError(f"Obsidian CLI binary not found: {configured}")
    discovered = shutil.which("obsidian")
    if discovered:
        return discovered
    for candidate in CLI_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise SetupError("Obsidian CLI not found; enable it in Obsidian Settings → General")


def _run_cli(binary: str, vault: str, arguments: list[str]) -> str:
    argv = [binary, f"vault={vault}", *arguments]
    try:
        process = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"failed to run Obsidian CLI: {exc}") from exc
    output = "\n".join(part.strip() for part in (process.stdout, process.stderr) if part.strip())
    error_line = next((line for line in output.splitlines() if line.startswith("Error:")), None)
    if process.returncode != 0 or error_line:
        raise SetupError(error_line or output or "Obsidian CLI command failed")
    return output


def _write_configuration(root: Path, min_update_minutes: int | None, community: bool) -> None:
    state = _documents(root, min_update_minutes)
    for label, (path, before, after) in state["files"].items():
        if label == "community_plugins" and not community:
            continue
        if before != after:
            _atomic_json_write(path, after)


def apply_setup(
    root: Path,
    vault: str | None,
    binary: str | None,
    configure_only: bool,
    min_update_minutes: int | None,
) -> dict[str, Any]:
    state = _documents(root, min_update_minutes)
    files_changed = any(before != after for _, before, after in state["files"].values())
    if state["plugin_installed"] and state["plugin_enabled"] and not files_changed:
        return {"applied": [], "backup_dir": None}

    paths = [entry[0] for entry in state["files"].values()]
    backup = _backup(root, paths)
    applied: list[str] = []

    if configure_only:
        if not state["plugin_installed"]:
            raise SetupError(
                f"{PLUGIN_ID} is not installed; omit --configure-only to install it via Obsidian CLI"
            )
        _write_configuration(root, min_update_minutes, community=True)
        applied.append("merged Obsidian JSON settings (restart Obsidian before use)")
    else:
        cli = _resolve_binary(binary)
        try:
            vault_name = vault or registry.vault_name_for_root(str(root))
        except Exception as exc:
            raise SetupError(str(exc)) from exc

        disabled_existing_plugin = False
        try:
            if not state["plugin_installed"]:
                _run_cli(cli, vault_name, ["plugins:restrict", "off"])
                _run_cli(cli, vault_name, ["plugin:install", f"id={PLUGIN_ID}", "enable"])
                applied.append(f"installed {PLUGIN_ID}")
            elif not state["plugin_enabled"]:
                _run_cli(cli, vault_name, ["plugins:restrict", "off"])

            if state["plugin_enabled"] or not state["plugin_installed"]:
                _run_cli(
                    cli,
                    vault_name,
                    ["plugin:disable", f"id={PLUGIN_ID}", "filter=community"],
                )
                disabled_existing_plugin = state["plugin_enabled"]
            _write_configuration(root, min_update_minutes, community=False)
            applied.append("merged plugin, property-type, core-plugin, and app settings")
            _run_cli(cli, vault_name, ["plugin:enable", f"id={PLUGIN_ID}", "filter=community"])
            disabled_existing_plugin = False
            _run_cli(cli, vault_name, ["reload"])
            applied.append(f"enabled {PLUGIN_ID} and reloaded Obsidian")
        except Exception as exc:
            if disabled_existing_plugin:
                try:
                    _run_cli(
                        cli,
                        vault_name,
                        ["plugin:enable", f"id={PLUGIN_ID}", "filter=community"],
                    )
                except SetupError as recovery:
                    raise SetupError(
                        f"{exc}; also failed to re-enable {PLUGIN_ID}: {recovery}"
                    ) from exc
            if isinstance(exc, SetupError):
                raise
            raise SetupError(f"failed to apply Obsidian settings: {exc}") from exc

    return {
        "applied": applied,
        "backup_dir": str(backup) if backup else None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--root", help="Obsidian vault root (default: config.yaml vault.root)")
    parser.add_argument("--vault", help="Obsidian vault name override for CLI commands")
    parser.add_argument("--binary", help="Obsidian CLI binary override")
    parser.add_argument("--apply", action="store_true", help="Apply the plan (default: dry-run)")
    parser.add_argument(
        "--configure-only",
        action="store_true",
        help="Do not invoke Obsidian CLI; plugin must already be installed and Obsidian must be closed",
    )
    parser.add_argument(
        "--min-update-minutes",
        type=int,
        default=None,
        help="Override plugin throttle; existing value is preserved when omitted (fresh default: 4)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.min_update_minutes is not None and args.min_update_minutes < 0:
            raise SetupError("--min-update-minutes must be zero or greater")
        configured_root = args.root or settings.vault_root()
        if not configured_root:
            raise SetupError("pass --root or set config.yaml vault.root")
        root = Path(configured_root).expanduser().resolve()
        if not root.is_dir() or not (root / ".obsidian").is_dir():
            raise SetupError(f"not an Obsidian vault: {root}")

        plan = build_plan(root, args.min_update_minutes)
        result: dict[str, Any] = {"dry_run": not args.apply, "plan": plan}
        if args.apply:
            result.update(
                apply_setup(
                    root,
                    args.vault,
                    args.binary,
                    args.configure_only,
                    args.min_update_minutes,
                )
            )
        result["vault_spider_config"] = {
            "timestamps.policy": "obsidian_local",
            "obsidian.manage_updated": False,
            "configured_by_this_script": False,
        }
        print(json.dumps({"ok": True, "action": "setup-obsidian", "result": result}))
        return 0
    except (OSError, SetupError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "action": "setup-obsidian",
                    "error": {"type": "setup_error", "message": str(exc)},
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
