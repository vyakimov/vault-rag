"""App-agnostic enrichment planner.

Given raw note text plus optional capture context, retrieve its neighborhood
and emit a structured *enrichment plan* (proposed title, metadata, links,
related notes, placement). This module reasons and proposes only — it NEVER
mutates files or the index. Applying a plan is the job of the CLI's note
mutation commands (vault_spider.obsidian).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

from nltk.stem import PorterStemmer

from vault_spider.synthesis.answer import parse_llm_json
from vault_spider.utils import DEFAULT_STOP_WORDS, tokenize_for_bm25

ALLOWED_TYPES = {
    "interview",
    "research",
    "recipe",
    "journal",
    "transcript",
    "idea",
    "project",
}


_SYSTEM_PROMPT = """You are an enrichment planner for a personal markdown knowledge vault.
Given a NOTE and its retrieved NEIGHBORS, propose conservative improvements as JSON.
Rules:
- Propose only what the text clearly supports. When unsure, leave fields out and add a warning instead.
- Only propose links to notes listed in NEIGHBORS.
- type must be one of: interview, research, recipe, journal, transcript, idea, project. Omit if unclear.
- aliases only when the note has an obvious alternate name. Never invent aliases.
- Do not rewrite or summarize the note. You are proposing metadata, links, and a title only.
Return JSON: {"title": str, "type": str|null, "aliases": [str],
 "inline_links": [{"target": str, "anchor_text": str, "confidence": 0.0-1.0}],
 "related": [{"target": str, "confidence": 0.0-1.0, "reason": str}],
 "warnings": [str]}"""


@dataclass
class EnrichInput:
    body: str
    title: str
    path: Optional[str]        # vault-relative path, or None for stdin
    existing_frontmatter: Dict[str, Any]
    given_title: Optional[str]
    intent: Optional[str]
    source_url: Optional[str]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _parse_confidence(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    return confidence


def _is_safe_title(title: str) -> bool:
    return bool(title) and title not in (".", "..") and not any(
        character in title for character in ("/", "\\", "\x00", "\n", "\r")
    )


def gather_neighbors(store, provider, inp: EnrichInput, per_query: int = 5, keep: int = 10):
    from vault_spider.retrieval.searcher import Searcher

    searcher = Searcher(store, granularity="document", provider=provider)
    stemmer = PorterStemmer()

    queries: List[str] = []
    if inp.title:
        queries.append(inp.title[:100])
    if inp.body.strip():
        queries.append(inp.body[:300])
    tokens = tokenize_for_bm25(inp.body, DEFAULT_STOP_WORDS, stemmer)
    if tokens:
        top_terms = [term for term, _ in Counter(tokens).most_common(5)]
        queries.append(" ".join(top_terms))

    merged: Dict[str, Dict[str, Any]] = {}
    for query in queries:
        if not query.strip():
            continue
        try:
            result = searcher.hybrid_search(
                query, mode="fast", granularity="document", n_results=per_query
            )
        except ValueError:
            continue
        for row in result.rows:
            metadata = row.get("metadata")
            if not isinstance(metadata, dict):
                continue
            path = str(metadata.get("path", ""))
            if inp.path and path == inp.path:
                continue
            note_id = str(row["note_id"])
            raw_score = row.get("final")
            if not isinstance(raw_score, (str, int, float)) or isinstance(raw_score, bool):
                continue
            try:
                score = float(raw_score)
            except ValueError:
                continue
            if not math.isfinite(score):
                continue
            if note_id not in merged or score > merged[note_id]["score"]:
                merged[note_id] = {
                    "note_id": note_id,
                    "title": str(metadata.get("title", "")),
                    "path": path,
                    "excerpt": str(row["document"])[:200],
                    "score": score,
                }
    return sorted(merged.values(), key=lambda n: n["score"], reverse=True)[:keep]


def build_prompts(inp: EnrichInput, neighbors: List[Dict[str, Any]]):
    note_block = f"{inp.title}\n{inp.body}"[:8000]
    neighbor_lines = "\n".join(
        f'- title="{n["title"]}" path="{n["path"]}" excerpt="{n["excerpt"][:200]}"'
        for n in neighbors
    )
    user_prompt = (
        f"NOTE:\n{note_block}\n\n"
        f"CONTEXT:\nintent={inp.intent or ''}\n\n"
        f"NEIGHBORS:\n{neighbor_lines}"
    )
    return _SYSTEM_PROMPT, user_prompt


class _NeighborIndex:
    def __init__(self, neighbors: List[Dict[str, Any]]):
        self.by_key: Dict[str, str] = {}
        for n in neighbors:
            path = n["path"]
            self.by_key.setdefault(n["title"].lower(), path)
            self.by_key.setdefault(PurePosixPath(path).stem.lower(), path)
            self.by_key.setdefault(path.lower(), path)
            if path.lower().endswith(".md"):
                self.by_key.setdefault(path[:-3].lower(), path)

    def resolve(self, target: str) -> Optional[str]:
        key = target.strip().lower()
        if key in self.by_key:
            return self.by_key[key]
        if not key.endswith(".md") and (key + ".md") in self.by_key:
            return self.by_key[key + ".md"]
        return None


def _first_line_containing(body: str, anchor: str) -> Optional[int]:
    for index, line in enumerate(body.split("\n"), start=1):
        if anchor in line:
            return index
    return None


def _suggested_path(inp: EnrichInput, title: str, neighbors: List[Dict[str, Any]]) -> str:
    top_folders = [PurePosixPath(n["path"]).parent.as_posix() for n in neighbors[:5]]
    consensus = None
    if top_folders:
        folder, count = Counter(top_folders).most_common(1)[0]
        if count >= 3 and folder not in ("", "."):
            consensus = folder

    is_stdin = inp.path is None
    current_folder = (
        "" if inp.path is None else PurePosixPath(inp.path).parent.as_posix()
    )
    inbox_like = "inbox" in current_folder.lower()

    if (is_stdin or inbox_like) and consensus:
        return f"{consensus}/{title}.md"
    if is_stdin:
        return f"Inbox/{title}.md"
    assert inp.path is not None
    return inp.path  # keep current location


def postprocess(
    parsed: Optional[Dict[str, Any]],
    inp: EnrichInput,
    neighbors: List[Dict[str, Any]],
) -> Dict[str, Any]:
    index = _NeighborIndex(neighbors)
    warnings: List[str] = []

    input_meta = {
        "path": inp.path,
        "given_title": inp.given_title,
        "intent": inp.intent,
    }

    # The LLM is not trusted: a non-object payload (e.g. a top-level array) is
    # treated as an unusable plan rather than crashing.
    if not isinstance(parsed, dict):
        parsed = None

    if parsed is None:
        return {
            "input": input_meta,
            "title": inp.title,
            "title_changed": False,
            "suggested_path": _suggested_path(inp, inp.title, neighbors),
            "frontmatter_patch": {},
            "link_insertions": [],
            "related_candidates": [],
            "warnings": ["planner failed to produce a usable plan"],
            "confidence": "low",
        }

    raw_warnings = parsed.get("warnings")
    if isinstance(raw_warnings, list):
        warnings.extend(str(w) for w in raw_warnings)

    # Title: unchanged if it differs only in case/punctuation from the current.
    fallback_title = inp.title if _is_safe_title(inp.title) else "Untitled"
    proposed_title = str(parsed.get("title") or fallback_title).strip() or fallback_title
    if not _is_safe_title(proposed_title):
        warnings.append("dropped title containing unsafe path characters")
        proposed_title = fallback_title
    title_changed = _norm(proposed_title) != _norm(inp.title)
    title = proposed_title if title_changed else inp.title

    related: List[Dict[str, Any]] = []
    related_seen = set()

    def add_related(target_path: str, target: str, confidence: float, reason: str):
        if target_path in related_seen:
            return
        related_seen.add(target_path)
        related.append(
            {"target": target, "target_path": target_path, "confidence": confidence, "reason": reason}
        )

    # Inline links.
    link_insertions: List[Dict[str, Any]] = []
    inline_paths = set()
    for entry in parsed.get("inline_links") or []:
        if not isinstance(entry, dict):
            continue
        target = str(entry.get("target", "")).strip()
        confidence = _parse_confidence(entry.get("confidence", 0.0))
        if confidence is None:
            warnings.append(f"dropped link with invalid confidence: {target}")
            continue
        resolved = index.resolve(target)
        if resolved is None:
            warnings.append(f"dropped link to nonexistent note: {target}")
            continue
        if f"[[{target}" in inp.body:
            continue  # already linked, no warning
        if confidence < 0.6:
            continue
        anchor = str(entry.get("anchor_text", "")).strip()
        line = _first_line_containing(inp.body, anchor) if anchor else None
        if not anchor or line is None:
            # Cannot place an inline link surgically -> demote to related.
            add_related(resolved, target, confidence, "anchor text not found in body")
            continue
        if confidence < 0.9:
            add_related(resolved, target, confidence, "medium-confidence inline link demoted")
            continue
        link_insertions.append(
            {
                "target": target,
                "target_path": resolved,
                "confidence": confidence,
                "mode": "inline",
                "anchor_text": anchor,
                "occurs_at_line": line,
            }
        )
        inline_paths.add(resolved)

    # Related candidates.
    for entry in parsed.get("related") or []:
        if not isinstance(entry, dict):
            continue
        target = str(entry.get("target", "")).strip()
        confidence = _parse_confidence(entry.get("confidence", 0.0))
        if confidence is None:
            warnings.append(f"dropped related with invalid confidence: {target}")
            continue
        resolved = index.resolve(target)
        if resolved is None:
            warnings.append(f"dropped related to nonexistent note: {target}")
            continue
        if resolved in inline_paths:
            continue  # inline wins
        if f"[[{target}" in inp.body:
            continue
        if confidence < 0.6:
            continue
        add_related(resolved, target, confidence, str(entry.get("reason", "")))

    # Frontmatter patch (only type/aliases/source_type/source_url; strip empties).
    patch: Dict[str, Any] = {}
    proposed_type = parsed.get("type")
    if proposed_type:
        proposed_type = str(proposed_type).strip().lower()
        existing_type = str(inp.existing_frontmatter.get("type") or "").strip().lower()
        if proposed_type not in ALLOWED_TYPES:
            warnings.append(f"dropped invalid type={proposed_type}")
        elif existing_type and existing_type != proposed_type:
            warnings.append(f"note already has type={existing_type}")
        elif not existing_type:
            patch["type"] = proposed_type

    raw_aliases = parsed.get("aliases")
    aliases = (
        [str(a).strip() for a in raw_aliases if str(a).strip()]
        if isinstance(raw_aliases, list)
        else []
    )
    if aliases:
        patch["aliases"] = aliases

    if inp.source_url:
        patch["source_url"] = inp.source_url

    # Overall confidence.
    if link_insertions and not warnings:
        confidence = "high"
    elif len(warnings) > 3:
        confidence = "low"
    else:
        confidence = "medium"

    return {
        "input": input_meta,
        "title": title,
        "title_changed": title_changed,
        "suggested_path": _suggested_path(inp, title, neighbors),
        "frontmatter_patch": patch,
        "link_insertions": link_insertions,
        "related_candidates": related,
        "warnings": warnings,
        "confidence": confidence,
    }


def plan(inp: EnrichInput, store, provider) -> Dict[str, Any]:
    neighbors = gather_neighbors(store, provider, inp)
    system_prompt, user_prompt = build_prompts(inp, neighbors)
    raw = provider.chat(system_prompt, user_prompt, temperature=0.2, max_tokens=2048)
    parsed = parse_llm_json(raw)
    return postprocess(parsed, inp, neighbors)
