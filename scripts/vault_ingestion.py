"""Utilities for reading Markdown notes from the vault."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

try:
    from utils import hash_string
except ImportError:
    from scripts.utils import hash_string


DATE_FILENAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\.md$")


def split_frontmatter(raw_text: str) -> Tuple[Dict[str, Any], str]:
    if not raw_text.startswith("---\n"):
        return {}, raw_text

    lines = raw_text.splitlines()
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            frontmatter = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :]).lstrip("\n")
            try:
                parsed = yaml.safe_load(frontmatter) or {}
                if isinstance(parsed, dict):
                    return parsed, body
            except yaml.YAMLError:
                return {}, raw_text
            return {}, body
    return {}, raw_text


def normalize_tags(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        separators = "," if "," in value else None
        parts = value.split(separators) if separators else value.split()
        return [part.strip() for part in parts if part.strip()]
    return [str(value).strip()]


def coerce_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min, tzinfo=dt.timezone.utc)
    if isinstance(value, str):
        candidate = value.strip().replace("Z", "+00:00")
        for parser in (dt.datetime.fromisoformat,):
            try:
                parsed = parser(candidate)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
        try:
            parsed_date = dt.date.fromisoformat(candidate)
            return dt.datetime.combine(parsed_date, dt.time.min, tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return None


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


def build_document_text(
    title: str,
    relative_path: str,
    tags: List[str],
    note_date: str,
    body: str,
) -> str:
    parts = [f"# {title}", f"Path: {relative_path}"]
    if tags:
        parts.append(f"Tags: {', '.join(tags)}")
    if note_date:
        parts.append(f"Date: {note_date}")
    if body.strip():
        parts.append(body.strip())
    return "\n\n".join(parts).strip()


def load_markdown_notes(vault_path: str) -> List[Dict[str, Dict[str, str] | str]]:
    root = Path(vault_path)
    if not root.exists():
        raise FileNotFoundError(f"Vault path not found: {vault_path}")

    note_documents: List[Dict[str, Dict[str, str] | str]] = []
    for path in sorted(root.rglob("*.md")):
        raw_text = path.read_text(encoding="utf-8", errors="ignore")
        frontmatter, body = split_frontmatter(raw_text)
        relative_path = path.relative_to(root).as_posix()
        title = str(frontmatter.get("title") or path.stem)
        tags = normalize_tags(frontmatter.get("tags"))
        note_date = resolve_note_date(path, frontmatter)
        document = build_document_text(title, relative_path, tags, note_date, body)
        note_documents.append(
            {
                "id": hash_string(relative_path),
                "document": document,
                "metadata": {
                    "title": title,
                    "path": relative_path,
                    "folder": path.parent.relative_to(root).as_posix(),
                    "tags": ", ".join(tags),
                    "date": note_date,
                    "source": "vault_markdown",
                },
            }
        )
    return note_documents
