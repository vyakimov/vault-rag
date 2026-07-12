"""Backfill id / created / updated frontmatter onto existing vault notes.

Safe by construction: existing metadata is preserved, note bodies are never
touched, and provenance is recorded in an external JSON report. Dry-run is the
default; writes happen only with --apply.

The timestamp policy comes from Phase 0 (plans/phase-0-results.md). Until that
file records a decision, the data contract's preferred format (UTC `Z`) is used;
change TIMESTAMP_POLICY here if Phase 0 selects offset-aware local time.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ulid import ULID

# Reuse the exact reading helpers the package already ships (identical behavior
# to the pre-refactor scripts/vault_ingestion.py the spec referenced).
from vault_rag.corpus.frontmatter import coerce_datetime, split_frontmatter

TIMESTAMP_POLICY = "offset_local"  # per plans/phase-0-results.md: "utc_z" | "offset_local"

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
SKIP_DIRS = {".trash", ".obsidian", "Templates", "999 Templates"}
LEGACY_ID_FIELDS = ("uid", "ulid", "luid")
CONTRACT_ORDER = ("id", "created", "updated")


# -- timestamp formatting -----------------------------------------------------

def format_timestamp(dt: datetime) -> str:
    if dt.tzinfo is None:
        # naive == local wall-clock (see coerce_datetime); attach local offset, not UTC
        dt = dt.astimezone()
    if TIMESTAMP_POLICY == "utc_z":
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # offset-aware local, colon in the offset (e.g. 2026-07-07T14:30:00+02:00)
    return dt.astimezone().isoformat(timespec="seconds")


def iso_to_policy(iso_string: str) -> str:
    return format_timestamp(datetime.fromisoformat(iso_string))


def now_timestamp() -> str:
    return format_timestamp(datetime.now(timezone.utc))


# -- git provenance -----------------------------------------------------------

@dataclass
class GitContext:
    inside: bool
    confidence: str  # "high" | "medium"


def git_context(root: Path) -> GitContext:
    try:
        inside = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True,
        ).stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return GitContext(False, "medium")
    if not inside:
        return GitContext(False, "medium")
    try:
        count = int(
            subprocess.run(
                ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
                capture_output=True, text=True,
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


# -- note model ---------------------------------------------------------------

@dataclass
class Change:
    field: str
    value: str
    source: str
    confidence: str
    warnings: List[str] = field(default_factory=list)


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
    # Frontmatter block present but unparseable.
    if raw.startswith("---\n") and not fm and _closing_fence(raw) is not None:
        return "frontmatter present but failed to parse", {}

    fm_id = str(fm["id"]).strip() if "id" in fm else None
    legacy = _legacy_id_values(fm)

    if fm_id is not None and legacy and any(v != fm_id for v in legacy.values()):
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


def resolve_created(fm: Dict[str, Any], root: Path, rel: str, git: GitContext, stat) -> Optional[Change]:
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
            "birthtime", "medium",
        )
    return Change(
        "created",
        format_timestamp(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)),
        "mtime", "low",
    )


def resolve_updated(fm: Dict[str, Any], root: Path, rel: str, git: GitContext, stat) -> Optional[Change]:
    if "updated" in fm:
        return None
    if git.inside:
        last = git_last_commit(root, rel)
        if last:
            return Change("updated", iso_to_policy(last), "git_last_commit", git.confidence)
    return Change(
        "updated",
        format_timestamp(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)),
        "mtime", "medium",
    )


# -- write mechanics ----------------------------------------------------------

def apply_changes_to_text(raw: str, changes: List[Change]) -> str:
    new_lines = [f"{change.field}: {change.value}" for change in changes]
    fence = _closing_fence(raw)
    if fence is not None:
        lines = raw.split("\n")
        return "\n".join(lines[:fence] + new_lines + lines[fence:])
    return "---\n" + "".join(f"{line}\n" for line in new_lines) + "---\n" + raw


# -- scanning -----------------------------------------------------------------

def _iter_files(root: Path, include_glob: str):
    for path in sorted(root.rglob(include_glob)):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        yield path, rel.as_posix()


@dataclass
class NoteRecord:
    path: Path
    rel: str
    raw: str
    fm: Dict[str, Any]


def build_report(root: Path, include_glob: str, apply: bool) -> Dict[str, Any]:
    git = git_context(root)

    records: List[NoteRecord] = []
    manual_review: List[Dict[str, Any]] = []
    id_counts: Dict[str, int] = {}

    for path, rel in _iter_files(root, include_glob):
        try:
            raw = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            manual_review.append({"path": rel, "reason": "file could not be decoded", "details": {}})
            continue
        fm, _ = split_frontmatter(raw)
        records.append(NoteRecord(path, rel, raw, fm))
        if "id" in fm:
            key = str(fm["id"]).strip()
            id_counts[key] = id_counts.get(key, 0) + 1

    changes: List[Dict[str, Any]] = []
    changed_notes = 0
    unchanged = 0

    for record in records:
        ambiguity = detect_ambiguity(record.fm, record.raw, id_counts)
        if ambiguity is not None:
            reason, details = ambiguity
            manual_review.append({"path": record.rel, "reason": reason, "details": details})
            continue

        stat = record.path.stat()
        note_changes = [
            change
            for change in (
                resolve_id(record.fm),
                resolve_created(record.fm, root, record.rel, git, stat),
                resolve_updated(record.fm, root, record.rel, git, stat),
            )
            if change is not None
        ]

        # Clamp: updated must not predate created. coerce_datetime attaches the
        # local offset to naive values so aware/naive comparisons cannot raise.
        by_field = {c.field: c for c in note_changes}
        created_val = by_field["created"].value if "created" in by_field else record.fm.get("created")
        if "updated" in by_field and created_val:
            updated_dt = coerce_datetime(by_field["updated"].value)
            created_dt = coerce_datetime(created_val)
            if updated_dt is not None and created_dt is not None and updated_dt < created_dt:
                by_field["updated"].value = _as_str(created_val)
                by_field["updated"].warnings.append("updated predated created; clamped to created")

        if not note_changes:
            unchanged += 1
            continue

        changed_notes += 1
        if apply:
            new_text = apply_changes_to_text(record.raw, note_changes)
            record.path.write_text(new_text, encoding="utf-8")

        for change in note_changes:
            changes.append(
                {
                    "path": record.rel,
                    "field": change.field,
                    "value": change.value,
                    "source": change.source,
                    "confidence": change.confidence,
                    "warnings": change.warnings,
                }
            )

    return {
        "root": str(root),
        "ran_at": now_timestamp(),
        "apply": apply,
        "totals": {
            "scanned": changed_notes + unchanged + len(manual_review),
            "changed": changed_notes,
            "skipped_unchanged": unchanged,
            "manual_review": len(manual_review),
        },
        "changes": changes,
        "manual_review": manual_review,
    }


def _as_str(value: Any) -> str:
    resolved = coerce_datetime(value)
    return format_timestamp(resolved) if resolved is not None else str(value)


# -- console output -----------------------------------------------------------

def print_summary(report: Dict[str, Any]) -> None:
    totals = report["totals"]
    print(f"Backfill {'APPLY' if report['apply'] else 'DRY-RUN'} — root: {report['root']}")
    print(
        f"  scanned={totals['scanned']} changed={totals['changed']} "
        f"unchanged={totals['skipped_unchanged']} manual_review={totals['manual_review']}"
    )

    per_source: Dict[Tuple[str, str], int] = {}
    for change in report["changes"]:
        per_source[(change["field"], change["source"])] = (
            per_source.get((change["field"], change["source"]), 0) + 1
        )
    if per_source:
        print("  changes by field/source:")
        for (field_name, source), count in sorted(per_source.items()):
            print(f"    {field_name:<8} {source:<16} {count}")

    if report["manual_review"]:
        print("  manual review needed:")
        for entry in report["manual_review"][:50]:
            print(f"    {entry['path']} — {entry['reason']}")

    if not report["apply"] and report["changes"]:
        print("  example diffs (dry-run, up to 10):")
        by_path: Dict[str, List[str]] = {}
        for change in report["changes"]:
            by_path.setdefault(change["path"], []).append(f"{change['field']}: {change['value']}")
        for path, lines in list(by_path.items())[:10]:
            print(f"    {path}")
            for line in lines:
                print(f"      + {line}")


# -- entry point --------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill id/created/updated frontmatter")
    parser.add_argument("--root", required=True, help="Vault directory to scan")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    parser.add_argument("--report", default=None, help="Report output path")
    parser.add_argument("--include-glob", default="*.md", help="Glob for files to scan")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"Error: root not found: {args.root}", file=sys.stderr)
        return 1

    if args.apply:
        print("WARNING: close Obsidian before applying (sync/iCloud writes can race).")

    try:
        report = build_report(root, args.include_glob, args.apply)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    report_path = args.report or f"backfill-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print_summary(report)
    print(f"Report written to {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
