"""Read-only corpus health report (no LLM, no writes, no Chroma dependency)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vault_rag.corpus.frontmatter import coerce_datetime, split_frontmatter
from vault_rag.corpus.loader import SKIP_DIR_PARTS, has_ignore_tag

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
TIMESTAMP_FIELDS = ("created", "updated", "date")
CONTRACT_FIELDS = ("id", "created", "updated")


@dataclass
class NoteInfo:
    path: str            # vault-relative posix
    stem: str
    title: str
    frontmatter: Dict[str, Any]
    body: str
    note_type: str
    recency: Optional[datetime] = field(default=None)


def _iter_note_files(root: Path):
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIR_PARTS for part in rel.parts[:-1]):
            continue
        yield path, rel.as_posix()


def extract_wikilinks(body: str) -> List[Tuple[str, int]]:
    """Return (target, 1-based line) for wikilinks outside fences and backticks."""
    results: List[Tuple[str, int]] = []
    in_fence = False
    for index, line in enumerate(body.split("\n"), start=1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        cleaned = INLINE_CODE_RE.sub("", line)
        for match in WIKILINK_RE.finditer(cleaned):
            results.append((match.group(1).strip(), index))
    return results


def _timestamp_problem(value: Any) -> Optional[str]:
    if coerce_datetime(value) is None:
        return "unparseable"
    if isinstance(value, datetime):
        return None if value.tzinfo else "naive"
    if isinstance(value, date):
        return "naive"
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.endswith("Z") or re.search(r"[+-]\d{2}:?\d{2}$", stripped):
            return None
        return "naive"
    return None


def _note_recency(frontmatter: Dict[str, Any]) -> Optional[datetime]:
    for key in ("updated", "date", "created"):
        resolved = coerce_datetime(frontmatter.get(key))
        if resolved is not None:
            return resolved
    return None


class _Resolver:
    def __init__(self, notes: List[NoteInfo]):
        self.by_title: Dict[str, str] = {}
        self.by_stem: Dict[str, str] = {}
        self.by_path: Dict[str, str] = {}
        for note in notes:
            self.by_title.setdefault(note.title.lower(), note.path)
            self.by_stem.setdefault(note.stem.lower(), note.path)
            self.by_path.setdefault(note.path.lower(), note.path)
            if note.path.lower().endswith(".md"):
                self.by_path.setdefault(note.path[:-3].lower(), note.path)

    def resolve(self, target: str) -> Optional[str]:
        key = target.strip().lower()
        if key in self.by_title:
            return self.by_title[key]
        if key in self.by_stem:
            return self.by_stem[key]
        if key in self.by_path:
            return self.by_path[key]
        if not key.endswith(".md") and (key + ".md") in self.by_path:
            return self.by_path[key + ".md"]
        return None


def _sources_wikilinks(body: str) -> List[Tuple[str, int]]:
    """Wikilinks under a `## Sources` heading (until the next heading or EOF)."""
    lines = body.split("\n")
    in_fence = False
    collecting = False
    collected: List[Tuple[str, int]] = []
    for index, line in enumerate(lines, start=1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            if collecting:
                continue
            continue
        if in_fence:
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            if collecting:
                break
            if heading.group(2).strip().lower() == "sources":
                collecting = True
            continue
        if collecting:
            cleaned = INLINE_CODE_RE.sub("", line)
            for match in WIKILINK_RE.finditer(cleaned):
                collected.append((match.group(1).strip(), index))
    return collected


def lint_vault(root: str) -> Dict[str, Any]:
    root_path = Path(root)
    notes: List[NoteInfo] = []
    notes_ignored = 0

    for path, rel in _iter_note_files(root_path):
        try:
            raw = path.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
        frontmatter, body = split_frontmatter(raw)
        if has_ignore_tag(body):
            notes_ignored += 1
            continue
        note = NoteInfo(
            path=rel,
            stem=Path(rel).stem,
            title=str(frontmatter.get("title") or Path(rel).stem),
            frontmatter=frontmatter,
            body=body,
            note_type=str(frontmatter.get("type") or ""),
            recency=_note_recency(frontmatter),
        )
        notes.append(note)

    resolver = _Resolver(notes)

    findings: Dict[str, List[Dict[str, Any]]] = {
        "missing_frontmatter_fields": [],
        "invalid_timestamps": [],
        "duplicate_ids": [],
        "broken_wikilinks": [],
        "orphans": [],
        "stale_distilled": [],
    }

    # missing_frontmatter_fields
    for note in notes:
        missing = [f for f in CONTRACT_FIELDS if f not in note.frontmatter]
        if missing:
            findings["missing_frontmatter_fields"].append({"path": note.path, "missing": missing})

    # invalid_timestamps
    for note in notes:
        for field_name in TIMESTAMP_FIELDS:
            if field_name not in note.frontmatter:
                continue
            problem = _timestamp_problem(note.frontmatter[field_name])
            if problem:
                findings["invalid_timestamps"].append(
                    {
                        "path": note.path,
                        "field": field_name,
                        "value": str(note.frontmatter[field_name]),
                        "problem": problem,
                    }
                )

    # duplicate_ids
    ids: Dict[str, List[str]] = {}
    for note in notes:
        raw_id = note.frontmatter.get("id")
        if raw_id is None:
            continue
        ids.setdefault(str(raw_id).strip(), []).append(note.path)
    for note_id, paths in ids.items():
        if len(paths) >= 2:
            findings["duplicate_ids"].append({"id": note_id, "paths": sorted(paths)})

    # broken_wikilinks + link graph
    outgoing: Dict[str, set] = {note.path: set() for note in notes}
    incoming: Dict[str, set] = {note.path: set() for note in notes}
    for note in notes:
        for target, line in extract_wikilinks(note.body):
            resolved = resolver.resolve(target)
            if resolved is None:
                findings["broken_wikilinks"].append(
                    {"path": note.path, "target": target, "line": line}
                )
            elif resolved != note.path:
                outgoing[note.path].add(resolved)
                incoming[resolved].add(note.path)

    # orphans (distilled notes excluded — their Sources always link out)
    for note in notes:
        if note.note_type == "distilled":
            continue
        if not outgoing[note.path] and not incoming[note.path]:
            findings["orphans"].append({"path": note.path})

    # stale_distilled
    for note in notes:
        if note.note_type != "distilled":
            continue
        stale_sources = []
        for target, _ in _sources_wikilinks(note.body):
            resolved = resolver.resolve(target)
            if resolved is None:
                findings["stale_distilled"].append(
                    {"path": note.path, "warning": f"unresolvable source link {target}"}
                )
                continue
            source = next((n for n in notes if n.path == resolved), None)
            if source is None or source.recency is None or note.recency is None:
                continue
            if source.recency > note.recency:
                stale_sources.append(
                    {
                        "source_path": source.path,
                        "source_updated": source.recency.isoformat(),
                        "distilled_updated": note.recency.isoformat(),
                    }
                )
        if stale_sources:
            findings["stale_distilled"].append(
                {"path": note.path, "stale_sources": stale_sources}
            )

    summary = {key: len(value) for key, value in findings.items()}
    return {
        "root": str(root),
        "notes_scanned": len(notes),
        "notes_ignored": notes_ignored,
        "summary": summary,
        "findings": findings,
    }
