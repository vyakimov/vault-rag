"""Read Obsidian's macOS vault registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from vault_rag.envelope import CliError


def registry_path() -> Path:
    return Path.home() / "Library/Application Support/obsidian/obsidian.json"


def registered_vaults() -> list[dict]:
    try:
        payload = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    vaults = payload.get("vaults", {})
    if not isinstance(vaults, dict):
        return []

    result: list[dict] = []
    for entry in vaults.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        result.append({"path": entry["path"], "open": entry.get("open") is True})
    return result


def active_vault_path() -> str:
    open_paths = [vault["path"] for vault in registered_vaults() if vault["open"]]
    if not open_paths:
        raise CliError(
            "invalid_arguments",
            "no active Obsidian vault; pass --root or set vault.root in config.yaml",
        )
    if len(open_paths) > 1:
        raise CliError(
            "ambiguous_target",
            "multiple active Obsidian vaults: " + ", ".join(open_paths),
            {"open_vault_paths": open_paths},
        )
    return open_paths[0]


def _vault_name(path: str) -> str:
    return Path(path).name


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return a.resolve() == b.resolve()


def vault_name_for_root(root: str) -> str:
    root_path = Path(root)
    resolved_root = root_path.resolve()
    records = [
        (vault["path"], Path(vault["path"]).resolve(), _vault_name(vault["path"]))
        for vault in registered_vaults()
    ]
    matches = [record for record in records if _same_path(record[1], root_path)]
    registered_paths = [record[0] for record in records]
    if not matches:
        raise CliError(
            "config_mismatch",
            f"vault root is not registered with Obsidian: {resolved_root}",
            {"root": str(resolved_root), "registered_paths": registered_paths},
        )

    name = matches[0][2]
    colliding_paths = sorted(
        {
            str(path)
            for _, path, record_name in records
            if record_name == name and not _same_path(path, root_path)
        }
    )
    if colliding_paths:
        raise CliError(
            "config_mismatch",
            f"Obsidian vault name collision for {name!r}; vault=<name> would be ambiguous",
            {
                "root": str(resolved_root),
                "vault_name": name,
                "colliding_paths": [str(resolved_root), *colliding_paths],
            },
        )
    return name


def vault_path_for_name(name: str) -> str:
    vaults = registered_vaults()
    matches = [vault["path"] for vault in vaults if _vault_name(vault["path"]) == name]
    if not matches:
        raise CliError(
            "config_mismatch",
            f"Obsidian vault is not registered: {name}",
            {"vault_name": name, "registered_paths": [v["path"] for v in vaults]},
        )
    if len(matches) > 1:
        raise CliError(
            "ambiguous_target",
            f"multiple registered Obsidian vaults are named {name!r}: " + ", ".join(matches),
            {"vault_name": name, "matching_paths": matches},
        )
    return matches[0]


def resolve_mutation_vault(
    explicit: Optional[str],
    configured_vault: Optional[str],
    configured_root: Optional[str],
) -> Optional[str]:
    if explicit is not None:
        name = explicit.strip()
        if not name:
            raise CliError("invalid_arguments", "--vault must not be empty")
        if registered_vaults():
            vault_path_for_name(name)
        return name

    if configured_vault is not None:
        registered_path = vault_path_for_name(configured_vault)
        if (
            configured_root is not None
            and not _same_path(Path(registered_path), Path(configured_root))
        ):
            raise CliError(
                "config_mismatch",
                "config.yaml obsidian.vault and vault.root point at different vaults",
                {
                    "obsidian_vault": configured_vault,
                    "obsidian_vault_path": str(Path(registered_path).resolve()),
                    "vault_root": str(Path(configured_root).resolve()),
                },
            )
        return configured_vault

    if configured_root is not None:
        return vault_name_for_root(configured_root)
    return None
