"""Backfill id / created / updated frontmatter onto existing vault notes.

Safe by construction: existing metadata is preserved, note bodies are never
touched, and provenance is recorded in an external JSON report. Dry-run is the
default; writes happen only with --apply.

The timestamp policy comes from config.yaml (`timestamps.policy`): `offset_local`
(the default), `utc_z`, or Obsidian-native `obsidian_local`.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Reuse the exact reading helpers the package already ships (identical behavior
# to the pre-refactor scripts/vault_ingestion.py the spec referenced).
from vault_spider.compounding.backfill_core import (
    LEGACY_ID_FIELDS,
    ULID_RE,
    Change,
    GitContext,
    _as_str,
    _closing_fence,
    _git_dates,
    _legacy_id_values,
    apply_changes_to_text,
    detect_ambiguity,
    format_timestamp,
    git_context,
    git_first_commit,
    git_last_commit,
    iso_to_policy,
    now_timestamp,
    resolve_created,
    resolve_id,
    resolve_updated,
)
from vault_spider.corpus.frontmatter import coerce_datetime, split_frontmatter
from vault_spider.corpus.loader import is_skipped_path

CONTRACT_ORDER = ("id", "created", "updated")

__all__ = [
    "ULID_RE", "LEGACY_ID_FIELDS", "format_timestamp",
    "iso_to_policy", "now_timestamp", "GitContext", "git_context", "_git_dates",
    "git_first_commit", "git_last_commit", "Change", "_legacy_id_values",
    "_closing_fence", "detect_ambiguity", "resolve_id", "resolve_created",
    "resolve_updated", "apply_changes_to_text", "_as_str", "build_report", "main",
]


# -- scanning -----------------------------------------------------------------

def _iter_files(root: Path, include_glob: str):
    for path in sorted(root.rglob(include_glob)):
        rel = path.relative_to(root)
        if is_skipped_path(rel):
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
