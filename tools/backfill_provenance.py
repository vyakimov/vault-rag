"""Backfill `provenance` frontmatter onto existing vault notes.

Provenance records who authored a note's words: `human` (typed by the vault
owner), `reference` (imported external human-authored content), `llm`
(imported LLM output), or `distilled` (generated from the vault, regenerable).

Heuristic mapping, applied only when `provenance` is absent:
  - type: distilled                          -> distilled
  - source_type: llm                         -> llm
  - source_url set, or source_type web/pdf   -> reference
  - everything else                          -> human

Safe by construction: bodies are never touched, existing `provenance` is never
changed (it is immutable once set), and file mtimes are preserved so the
metadata-only edit does not register as recency. Dry-run is the default;
writes happen only with --apply. Review the dry-run report before applying —
the `human` default is intentionally conservative and pasted-LLM notes are the
cases worth reclassifying by hand afterwards (create-time capture should set
provenance explicitly going forward).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List

from vault_spider.corpus.frontmatter import split_frontmatter
from vault_spider.corpus.loader import is_skipped_path

VALUES = ("human", "reference", "llm", "distilled")


def derive_provenance(frontmatter: Dict) -> str:
    if str(frontmatter.get("type") or "").strip().lower() == "distilled":
        return "distilled"
    source_type = str(frontmatter.get("source_type") or "").strip().lower()
    if source_type == "llm":
        return "llm"
    if str(frontmatter.get("source_url") or "").strip() or source_type in ("web", "pdf"):
        return "reference"
    return "human"


def stamp(raw: str, value: str) -> str:
    """Insert `provenance: value` as the last frontmatter line. Body untouched."""
    lines = raw.splitlines(keepends=True)
    # split_frontmatter already validated shape; find the closing fence.
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            newline = "\n"
            return "".join(
                lines[:index] + [f"provenance: {value}{newline}"] + lines[index:]
            )
    raise ValueError("frontmatter closing fence not found")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill provenance frontmatter")
    parser.add_argument("--root", required=True, help="Vault directory")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    parser.add_argument("--report", default=None, help="Write a JSON report to this path")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"error: root directory not found: {root}", file=sys.stderr)
        return 1

    planned: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    counts: Dict[str, int] = {value: 0 for value in VALUES}

    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if is_skipped_path(rel):
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            skipped.append({"path": rel.as_posix(), "reason": "not utf-8"})
            continue
        frontmatter, _ = split_frontmatter(raw)
        if not frontmatter:
            skipped.append({"path": rel.as_posix(), "reason": "no frontmatter"})
            continue
        existing = str(frontmatter.get("provenance") or "").strip().lower()
        if existing:
            reason = (
                "already set" if existing in VALUES else f"unknown value kept: {existing}"
            )
            skipped.append({"path": rel.as_posix(), "reason": reason})
            continue
        value = derive_provenance(frontmatter)
        counts[value] += 1
        planned.append({"path": rel.as_posix(), "provenance": value})
        if args.apply:
            stat = path.stat()
            path.write_text(stamp(raw, value), encoding="utf-8")
            os.utime(path, (stat.st_atime, stat.st_mtime))

    report = {
        "root": str(root),
        "applied": bool(args.apply),
        "planned": len(planned),
        "by_provenance": counts,
        "skipped": len(skipped),
        "changes": planned,
        "skipped_details": skipped,
    }
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    summary = {key: report[key] for key in ("root", "applied", "planned", "by_provenance", "skipped")}
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not args.apply:
        print("dry-run only; re-run with --apply to write", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
