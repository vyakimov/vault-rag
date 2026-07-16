"""Load Markdown notes from the vault into ``Note`` objects."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Set

from vault_spider import settings
from vault_spider.corpus.frontmatter import coerce_datetime, normalize_tags, split_frontmatter
from vault_spider.corpus.identity import resolve_note_id

DATE_FILENAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\.md$")

def ignore_tags() -> List[str]:
    """Tags that mean "never index this note". Configurable: `vault.ignore_tags`."""
    return settings.ignore_tags()


def _ignore_tag_re() -> re.Pattern:
    tags = ignore_tags() or ["ignore"]
    alternation = "|".join(re.escape(tag) for tag in tags)
    return re.compile(rf"(?<!\S)#(?:{alternation})(?![\w/-])", re.IGNORECASE)


def skip_dirs() -> Set[str]:
    """Vault-relative directory names that are never indexed.

    Configurable via `vault.skip_dirs` — a vault whose Templater folder is called
    "999 Templates" adds it there rather than to this source file.
    """
    return settings.skip_dirs()

# Excalidraw stores drawings as `.md` files whose body is compressed drawing data,
# not prose. Embedding them poisons retrieval, so they are skipped everywhere.
EXCALIDRAW_SUFFIX = ".excalidraw.md"
EXCALIDRAW_FRONTMATTER_KEY = "excalidraw-plugin"


@dataclass
class Note:
    note_id: str
    path: str            # vault-relative posix path
    title: str
    tags: List[str]
    created: str | None  # from frontmatter `created`, ISO string or None
    updated: str | None  # from frontmatter `updated`, ISO string or None
    date: str            # resolved display/recency date
    note_type: str       # frontmatter `type` or ""
    body: str            # body without frontmatter
    raw_text: str        # full original file text
    content_hash: str    # sha256 hexdigest of raw_text


def has_ignore_tag(body: str) -> bool:
    return bool(_ignore_tag_re().search(body))


def has_ignore_frontmatter_tag(tags: List[str]) -> bool:
    """True when a frontmatter tag is `ignore`/`secret` (with or without `#`)."""
    ignored = ignore_tags()
    return any(tag.lstrip("#").lower() in ignored for tag in tags)


def resolve_note_date(path: Path, frontmatter: Dict[str, Any]) -> str:
    for key in ("date", "created"):
        resolved = coerce_datetime(frontmatter.get(key))
        if resolved is not None:
            return resolved.isoformat()

    match = DATE_FILENAME_RE.match(path.name)
    if match:
        resolved = dt.datetime.fromisoformat(match.group("date")).replace(
            tzinfo=dt.timezone.utc
        )
        return resolved.isoformat()

    modified = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    return modified.isoformat()


def _iso_or_none(value: Any) -> str | None:
    resolved = coerce_datetime(value)
    return resolved.isoformat() if resolved is not None else None


def is_skipped_path(relative_path: Path) -> bool:
    """True when any parent directory is skipped or hidden.

    Hidden directories (`.git`, plugin caches, ...) are never part of the vault:
    Obsidian does not index dot-folders, so nothing inside one is a note or a
    linkable attachment.
    """
    skipped = skip_dirs()
    return any(
        part in skipped or part.startswith(".") for part in relative_path.parts[:-1]
    )


def is_excalidraw(relative_path: Path, frontmatter: Dict[str, Any]) -> bool:
    return (
        relative_path.name.lower().endswith(EXCALIDRAW_SUFFIX)
        or EXCALIDRAW_FRONTMATTER_KEY in frontmatter
    )


def load_notes(root: str) -> List[Note]:
    root_path = Path(root)
    if not root_path.exists():
        raise FileNotFoundError(f"Vault path not found: {root}")

    notes: List[Note] = []
    for path in sorted(root_path.rglob("*.md")):
        relative_path = path.relative_to(root_path)
        if is_skipped_path(relative_path):
            continue
        try:
            raw_text = path.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            # Same policy as lint: skip files that are not valid UTF-8 instead
            # of indexing silently mangled text.
            continue
        frontmatter, body = split_frontmatter(raw_text)
        tags = normalize_tags(frontmatter.get("tags"))
        if has_ignore_tag(body) or has_ignore_frontmatter_tag(tags):
            continue
        if is_excalidraw(relative_path, frontmatter):
            continue
        relative_posix = relative_path.as_posix()
        title = str(frontmatter.get("title") or path.stem)
        note = Note(
            note_id=resolve_note_id(frontmatter, relative_posix),
            path=relative_posix,
            title=title,
            tags=tags,
            created=_iso_or_none(frontmatter.get("created")),
            updated=_iso_or_none(frontmatter.get("updated")),
            date=resolve_note_date(path, frontmatter),
            note_type=str(frontmatter.get("type") or ""),
            body=body,
            raw_text=raw_text,
            content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        )
        notes.append(note)
    return notes
