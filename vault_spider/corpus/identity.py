"""Note identity resolution: frontmatter `id` (ULID) with a path-hash fallback."""

from __future__ import annotations

import re
from typing import Any, Dict

from vault_spider.utils import hash_string

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def is_ulid(value: Any) -> bool:
    return isinstance(value, str) and bool(ULID_RE.match(value))


def resolve_note_id(frontmatter: Dict[str, Any], relative_path: str) -> str:
    """Frontmatter `id` (any non-empty scalar, stripped) if present, else hash_string(relative_path)."""
    raw = frontmatter.get("id")
    if raw is not None and not isinstance(raw, (list, dict)):
        candidate = str(raw).strip()
        if candidate:
            return candidate
    return hash_string(relative_path)
