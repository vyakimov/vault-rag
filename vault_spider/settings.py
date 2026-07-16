"""Installation settings, read from an optional ``config.yaml``.

Everything installation-specific belongs here rather than in the source: where the vault
is, which folders to skip, which tags mean "never index", the timestamp policy. That is
what keeps the code publishable — no one's personal paths are baked into it.

The file is **optional**: without it the built-in defaults apply, and they are deliberately
vault-agnostic, so a fresh clone works with no config at all. Copy ``config.yaml.example``
to ``config.yaml`` (gitignored) and edit. Secrets stay in ``.env`` — never here.

``VAULT_SPIDER_CONFIG`` overrides the path, for setups that keep the file elsewhere.

Not to be confused with :mod:`vault_spider.config`, which holds retrieval/ranking tuning
constants that are part of the algorithm, not of an installation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"

# Built-in defaults. No personal paths, no vault-specific folder names.
DEFAULTS: Dict[str, Dict[str, Any]] = {
    "vault": {
        "root": None,  # unset falls back to the active vault in Obsidian's registry
        "skip_dirs": [".trash", ".obsidian", "Templates"],
        "ignore_tags": ["ignore", "secret"],
        "distilled_dir": "Distilled",
    },
    "index": {
        "chroma_path": "chroma_db",
    },
    "timestamps": {
        "policy": "offset_local",  # or "utc_z"
    },
    # Connection facts for the mutation backend (the official Obsidian CLI).
    # Facts only, no workflow policy: which binary, which vault, and whether
    # this installation's modified-date plugin is absent (manage_updated).
    "obsidian": {
        "binary": None,  # auto-discovered when unset
        "vault": None,  # unset maps vault.root, then falls back to the active vault
        "manage_updated": False,  # true only if no plugin maintains `updated`
    },
}


class ConfigError(ValueError):
    """config.yaml exists but is unusable. Surfaced as `invalid_arguments`."""


def config_path() -> Path:
    override = os.environ.get("VAULT_SPIDER_CONFIG")
    return Path(override).expanduser().absolute() if override else DEFAULT_CONFIG_PATH


_cache: Optional[Dict[str, Dict[str, Any]]] = None


def reset() -> None:
    """Drop the cached config, so the next read re-parses the file."""
    global _cache
    _cache = None


def load(refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """Defaults, overlaid with ``config.yaml`` when it exists."""
    global _cache
    if _cache is not None and not refresh:
        return _cache

    merged = {section: dict(values) for section, values in DEFAULTS.items()}
    path = config_path()
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
        if raw is not None:
            if not isinstance(raw, dict):
                raise ConfigError(f"{path} must be a mapping, got {type(raw).__name__}")
            for section, values in raw.items():
                if section not in merged:
                    raise ConfigError(
                        f"{path}: unknown section {section!r} "
                        f"(known: {', '.join(sorted(merged))})"
                    )
                if not isinstance(values, dict):
                    raise ConfigError(f"{path}: section {section!r} must be a mapping")
                unknown = sorted(set(values) - set(merged[section]))
                if unknown:
                    raise ConfigError(
                        f"{path}: unknown key(s) in {section!r}: {', '.join(unknown)}"
                    )
                merged[section].update(values)

    _cache = merged
    return merged


def _get(section: str, key: str) -> Any:
    return load()[section][key]


def _config_local_path(value: Any) -> str:
    """Expand a configured path and anchor relative values beside config.yaml."""
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return str(path)
    return str(config_path().parent / path)


def vault_root() -> Optional[str]:
    root = _get("vault", "root")
    return _config_local_path(root) if root else None


def skip_dirs() -> Set[str]:
    return set(_get("vault", "skip_dirs") or [])


def ignore_tags() -> List[str]:
    return [str(tag).lstrip("#").lower() for tag in (_get("vault", "ignore_tags") or [])]


def distilled_dir() -> str:
    return str(_get("vault", "distilled_dir"))


def chroma_path() -> str:
    return _config_local_path(_get("index", "chroma_path"))


def obsidian_binary() -> Optional[str]:
    binary = _get("obsidian", "binary")
    return _config_local_path(binary) if binary else None


def obsidian_vault() -> Optional[str]:
    vault = _get("obsidian", "vault")
    return str(vault) if vault else None


def obsidian_manage_updated() -> bool:
    return bool(_get("obsidian", "manage_updated"))


def timestamp_policy() -> str:
    policy = str(_get("timestamps", "policy"))
    if policy not in ("offset_local", "utc_z"):
        raise ConfigError(
            f"timestamps.policy must be 'offset_local' or 'utc_z', got {policy!r}"
        )
    return policy
