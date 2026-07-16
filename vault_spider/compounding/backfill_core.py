"""Shared frontmatter backfill resolution and write mechanics."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ulid import ULID

from vault_spider import settings
from vault_spider.corpus.frontmatter import coerce_datetime

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
LEGACY_ID_FIELDS = ("uid", "ulid", "luid")


def format_timestamp(dt: datetime) -> str:
    """Render a timestamp per the configured policy (`timestamps.policy`)."""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    if settings.timestamp_policy() == "utc_z":
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.astimezone().isoformat(timespec="seconds")


def iso_to_policy(iso_string: str) -> str:
    return format_timestamp(datetime.fromisoformat(iso_string))


def now_timestamp() -> str:
    return format_timestamp(datetime.now(timezone.utc))


def fresh_identity() -> Dict[str, str]:
    stamp = now_timestamp()
    return {"id": str(ULID()), "created": stamp, "updated": stamp}


@dataclass
class GitContext:
    inside: bool
    confidence: str


def git_context(root: Path) -> GitContext:
    try:
        inside = (
            subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            == "true"
        )
    except (OSError, subprocess.SubprocessError):
        return GitContext(False, "medium")
    if not inside:
        return GitContext(False, "medium")
    try:
        count = int(
            subprocess.run(
                ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            or "0"
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        count = 0
    return GitContext(True, "high" if count > 50 else "medium")


def _git_dates(root: Path, rel: str, first_added: bool) -> List[str]:
    args = ["git", "-C", str(root), "log", "--follow"]
    if first_added:
        args.append("--diff-filter=A")
    args += ["--format=%aI", "--", rel]
    try:
        out = subprocess.run(args, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.splitlines() if line.strip()]


def git_first_commit(root: Path, rel: str) -> Optional[str]:
    lines = _git_dates(root, rel, first_added=True)
    if not lines:
        lines = _git_dates(root, rel, first_added=False)
    return lines[-1] if lines else None


def git_last_commit(root: Path, rel: str) -> Optional[str]:
    lines = _git_dates(root, rel, first_added=False)
    return lines[0] if lines else None


@dataclass
class Change:
    field: str
    value: str
    source: str
    confidence: str
    warnings: List[str] = dataclass_field(default_factory=list)


def _legacy_id_values(fm: Dict[str, Any]) -> Dict[str, str]:
    return {
        key: str(fm[key]).strip()
        for key in LEGACY_ID_FIELDS
        if key in fm and str(fm[key]).strip()
    }


def _closing_fence(raw: str) -> Optional[int]:
    if not raw.startswith("---\n"):
        return None
    lines = raw.split("\n")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index
    return None


def detect_ambiguity(
    fm: Dict[str, Any], raw: str, id_counts: Dict[str, int]
) -> Optional[Tuple[str, Dict[str, Any]]]:
    if raw.startswith("---\n") and not fm and _closing_fence(raw) is not None:
        return "frontmatter present but failed to parse", {}
    fm_id = str(fm["id"]).strip() if "id" in fm else None
    legacy = _legacy_id_values(fm)
    if fm_id is not None and legacy and any(value != fm_id for value in legacy.values()):
        return "id and legacy identifier disagree", {"id": fm_id, **legacy}
    if len(set(legacy.values())) > 1:
        return "multiple legacy identifiers disagree", dict(legacy)
    for ts_field in ("created", "updated", "date"):
        if ts_field in fm and coerce_datetime(fm[ts_field]) is None:
            return f"{ts_field} value does not parse", {ts_field: str(fm[ts_field])}
    if fm_id is not None and id_counts.get(fm_id, 0) >= 2:
        return "duplicate id shared across notes", {"id": fm_id}
    return None


def resolve_id(fm: Dict[str, Any]) -> Optional[Change]:
    if "id" in fm:
        return None
    legacy = _legacy_id_values(fm)
    if legacy:
        value = next(iter(legacy.values()))
        warnings = [] if ULID_RE.match(value) else ["legacy id is not a ULID"]
        return Change("id", value, "legacy_field", "high", warnings)
    return Change("id", str(ULID()), "generated", "high")


def resolve_created(
    fm: Dict[str, Any], root: Path, rel: str, git: GitContext, stat
) -> Optional[Change]:
    if "created" in fm:
        return None
    date_value = coerce_datetime(fm["date"]) if "date" in fm else None
    if date_value is not None:
        return Change("created", format_timestamp(date_value), "legacy_field", "high")
    if git.inside:
        first = git_first_commit(root, rel)
        if first:
            return Change("created", iso_to_policy(first), "git_first_commit", git.confidence)
    birthtime = getattr(stat, "st_birthtime", None)
    if birthtime is not None:
        return Change(
            "created",
            format_timestamp(datetime.fromtimestamp(birthtime, tz=timezone.utc)),
            "birthtime",
            "medium",
        )
    return Change(
        "created",
        format_timestamp(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)),
        "mtime",
        "low",
    )


def resolve_updated(
    fm: Dict[str, Any], root: Path, rel: str, git: GitContext, stat
) -> Optional[Change]:
    if "updated" in fm:
        return None
    if git.inside:
        last = git_last_commit(root, rel)
        if last:
            return Change("updated", iso_to_policy(last), "git_last_commit", git.confidence)
    return Change(
        "updated",
        format_timestamp(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)),
        "mtime",
        "medium",
    )


def apply_changes_to_text(raw: str, changes: List[Change]) -> str:
    new_lines = [f"{change.field}: {change.value}" for change in changes]
    fence = _closing_fence(raw)
    if fence is not None:
        lines = raw.split("\n")
        return "\n".join(lines[:fence] + new_lines + lines[fence:])
    return "---\n" + "".join(f"{line}\n" for line in new_lines) + "---\n" + raw


def _as_str(value: Any) -> str:
    resolved = coerce_datetime(value)
    return format_timestamp(resolved) if resolved is not None else str(value)
