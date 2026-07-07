"""Load Markdown notes from the vault into ``Note`` objects."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from vault_rag.corpus.frontmatter import coerce_datetime, normalize_tags, split_frontmatter
from vault_rag.corpus.identity import resolve_note_id

DATE_FILENAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\.md$")

IGNORE_TAGS = ("ignore", "secret")
IGNORE_TAG_RE = re.compile(
    r"(?<!\S)#(?:" + "|".join(IGNORE_TAGS) + r")(?![\w/-])",
    re.IGNORECASE,
)

# Vault-relative directory prefixes that are never indexed. "999 Templates" is
# this vault's Templater folder (recorded in plans/phase-0-results.md).
SKIP_DIR_PARTS = {".trash", ".obsidian", "Templates", "999 Templates"}


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
    return bool(IGNORE_TAG_RE.search(body))


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


def _is_skipped(relative_path: Path) -> bool:
    return any(part in SKIP_DIR_PARTS for part in relative_path.parts[:-1])


def load_notes(root: str) -> List[Note]:
    root_path = Path(root)
    if not root_path.exists():
        raise FileNotFoundError(f"Vault path not found: {root}")

    notes: List[Note] = []
    for path in sorted(root_path.rglob("*.md")):
        relative_path = path.relative_to(root_path)
        if _is_skipped(relative_path):
            continue
        raw_text = path.read_text(encoding="utf-8", errors="ignore")
        frontmatter, body = split_frontmatter(raw_text)
        if has_ignore_tag(body):
            continue
        relative_posix = relative_path.as_posix()
        title = str(frontmatter.get("title") or path.stem)
        tags = normalize_tags(frontmatter.get("tags"))
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
