"""Frontmatter parsing and normalization helpers."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Tuple

import yaml


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
    # A naive timestamp in frontmatter is local wall-clock time — that is how
    # Obsidian/Templater and humans write it (e.g. a daily note's "date: ...T15:10").
    # Attach the local offset via astimezone(), which honors historical DST from the
    # OS tz database; never assume UTC, or the wall-clock time shifts by the offset.
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.astimezone()
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min).astimezone()
    if isinstance(value, str):
        candidate = value.strip().replace("Z", "+00:00")
        for parser in (dt.datetime.fromisoformat,):
            try:
                parsed = parser(candidate)
                return parsed if parsed.tzinfo else parsed.astimezone()
            except ValueError:
                continue
        try:
            parsed_date = dt.date.fromisoformat(candidate)
            return dt.datetime.combine(parsed_date, dt.time.min).astimezone()
        except ValueError:
            return None
    return None
