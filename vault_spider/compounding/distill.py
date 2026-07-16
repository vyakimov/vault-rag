"""Persist a good synthesis answer as a distilled note in the vault.

A distilled note is evidence about its *sources*, never about the world. Raw
notes always win on conflict; distilled notes are regenerable derived artifacts.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from vault_spider.compounding.backfill_core import fresh_identity
from vault_spider.utils import validate_vault_relative_path


class EmptySlugError(ValueError):
    """Raised when a question slugifies to an empty string."""


class InvalidSaveDirectoryError(ValueError):
    """Raised when a distilled-note destination is not inside the vault."""


def slugify(question: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", question.lower())
    slug = slug.strip("-")[:80].strip("-")
    return slug


def _unique_sources(citations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One entry per cited note (dedupe by note_id, keep first heading)."""
    seen = set()
    unique: List[Dict[str, Any]] = []
    for citation in citations:
        note_id = citation.get("note_id")
        if note_id in seen:
            continue
        seen.add(note_id)
        unique.append(citation)
    return unique


def _link_targets(sources: List[Dict[str, Any]]) -> Dict[str, str]:
    """Map each source's note_id to its wikilink target.

    Title is the target unless two cited notes share a title, in which case the
    vault-relative path (without .md) is used so Obsidian resolves uniquely.
    """
    title_counts: Dict[str, int] = {}
    for source in sources:
        title_counts[source.get("title", "")] = title_counts.get(source.get("title", ""), 0) + 1

    targets: Dict[str, str] = {}
    for source in sources:
        title = source.get("title", "")
        if title and title_counts.get(title, 0) == 1:
            targets[source.get("note_id", "")] = title
        else:
            path = str(source.get("path", ""))
            targets[source.get("note_id", "")] = path[:-3] if path.endswith(".md") else path
    return targets


def render_distilled_note(synth_output: Dict[str, Any]) -> str:
    question = str(synth_output.get("question", "")).strip()
    answer = str(synth_output.get("answer", "")).strip()
    sources = _unique_sources(synth_output.get("citations", []) or [])
    targets = _link_targets(sources)

    identity = fresh_identity()
    lines = [
        "---",
        f"id: {identity['id']}",
        f"created: {identity['created']}",
        f"updated: {identity['updated']}",
        "type: distilled",
        "---",
        f"# {question}",
        "",
        answer,
        "",
        "## Sources",
    ]
    for source in sources:
        target = targets.get(source.get("note_id", ""), source.get("title", ""))
        heading = " ".join(str(source.get("heading", "")).split())
        excerpt = " ".join(str(source.get("excerpt", "")).split())
        if heading:
            lines.append(f"- [[{target}]] — {heading}: {excerpt[:120]}")
        else:
            lines.append(f"- [[{target}]]")
    return "\n".join(lines) + "\n"


def save_distilled_note(
    synth_output: Dict[str, Any],
    root: str,
    save_dir: str = "Distilled",
) -> Dict[str, Any]:
    """Attempt to persist ``synth_output`` as a distilled note.

    Returns ``{"saved": bool, "saved_path": str|None, "warnings": [...]}``.
    Raises ``EmptySlugError`` when the question slugifies to empty.
    """
    question = str(synth_output.get("question", ""))
    slug = slugify(question)
    if not slug:
        raise EmptySlugError("question slugifies to an empty string")

    try:
        relative_dir = validate_vault_relative_path(save_dir, label="save directory")
    except ValueError as exc:
        raise InvalidSaveDirectoryError(str(exc)) from exc

    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise InvalidSaveDirectoryError(f"vault root directory not found: {root}")

    # Skip conditions, checked in order.
    if synth_output.get("abstained"):
        return {"saved": False, "saved_path": None, "warnings": ["not saved: model abstained"]}
    if str(synth_output.get("confidence", "")).lower() == "low":
        return {"saved": False, "saved_path": None, "warnings": ["not saved: low confidence"]}
    if not str(synth_output.get("answer", "")).strip():
        return {"saved": False, "saved_path": None, "warnings": ["not saved: empty answer"]}
    if not (synth_output.get("citations") or []):
        return {"saved": False, "saved_path": None, "warnings": ["not saved: no citations"]}

    rel_path = f"{relative_dir}/{slug}.md"
    target = (root_path / relative_dir / f"{slug}.md").resolve()
    try:
        target.relative_to(root_path)
    except ValueError as exc:
        raise InvalidSaveDirectoryError("save directory resolves outside the vault root") from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8") as handle:
            handle.write(render_distilled_note(synth_output))
    except FileExistsError:
        return {
            "saved": False,
            "saved_path": None,
            "warnings": [f"not saved: {rel_path} already exists"],
        }
    return {"saved": True, "saved_path": rel_path, "warnings": []}
