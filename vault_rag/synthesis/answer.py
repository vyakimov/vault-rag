"""Turn a retrieval output contract into an LLM-synthesized answer.

Kept free of Streamlit imports so both the CLI and the Streamlit UI share the
same prompt, context assembly, and JSON-parsing logic.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from vault_rag.llm.openrouter import OpenRouterClient

_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_code_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text).strip()


def _try_repair_truncated_json(text: str) -> Optional[Dict[str, Any]]:
    # Reasoning models can exhaust max_tokens mid-output, leaving JSON
    # truncated. Close any open string and append matching braces/brackets
    # so json.loads can recover the partial answer.
    in_string = False
    escape = False
    stack: List[str] = []
    for char in text:
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and stack and stack[-1] == char:
            stack.pop()
    repaired = text
    if in_string:
        repaired += '"'
    while stack:
        repaired += stack.pop()
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


def parse_llm_json(response: str) -> Optional[Dict[str, Any]]:
    if not response:
        return None
    candidate = _strip_code_fences(response)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}") + 1
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start:end])
        except json.JSONDecodeError:
            pass
    if start != -1:
        repaired = _try_repair_truncated_json(candidate[start:])
        if repaired is not None:
            return repaired
    return None


_SYSTEM_PROMPT = """You are a retrieval-grounded assistant.
Use only the note excerpts provided in <CONTEXT>.
Every factual claim must cite one or more note ids like [S0] or [S0, S1].
If the notes do not contain enough information, say that clearly.
If the notes do not contain enough information to answer, set "abstained": true and say what is missing.
Some context notes may be marked type=distilled: these are machine-written summaries of other notes. Treat them as pointers, not primary evidence — when a distilled note conflicts with a raw note, trust the raw note.
Return JSON with this exact shape:
{
  "answer": "<text>",
  "citations": ["S0"],
  "confidence": "High|Medium|Low",
  "abstained": true|false
}
"""


def generate_prompts(query: str, context: str) -> Tuple[str, str]:
    user_prompt = f"""<QUERY>
{query}
</QUERY>
<CONTEXT>
{context}
</CONTEXT>
Answer using only the context above."""
    return _SYSTEM_PROMPT, user_prompt


def build_context(
    retrieval_output: Dict[str, Any], hard_cutoff: int = 8
) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    context_parts: List[str] = []
    index_map: Dict[str, Dict[str, Any]] = {}
    for candidate in retrieval_output.get("candidates", []):
        citation_key = f"S{len(index_map)}"
        final_score = float(candidate.get("scores", {}).get("final", 0.0))
        type_attr = " type=distilled" if candidate.get("type") == "distilled" else ""
        context_parts.append(
            "\n".join(
                [
                    f"<{citation_key}{type_attr} score={final_score:.4f}>",
                    f"Title: {candidate.get('title', '(untitled)')}",
                    f"Path: {candidate.get('path', '')}",
                    candidate.get("excerpt", ""),
                    f"</{citation_key}>",
                ]
            )
        )
        index_map[citation_key] = candidate
        if len(index_map) >= hard_cutoff:
            break
    return "\n\n".join(context_parts), index_map


def synthesize(
    client: OpenRouterClient,
    retrieval_output: Dict[str, Any],
    question: Optional[str] = None,
    hard_cutoff: int = 8,
    max_tokens: int = 4096,
) -> Dict[str, Any]:
    question = question or str(retrieval_output.get("query", ""))
    context, index_map = build_context(retrieval_output, hard_cutoff=hard_cutoff)
    system_prompt, user_prompt = generate_prompts(question, context)
    raw = client.chat(system_prompt, user_prompt, max_tokens=max_tokens)
    parsed = parse_llm_json(raw)

    if parsed is None:
        return {
            "question": question,
            "answer": "",
            "confidence": "low",
            "abstained": True,
            "citations": [],
            "notes_used": [],
            "warnings": ["unparseable model output"],
            "raw": raw,
        }

    warnings: List[str] = []
    citations: List[Dict[str, Any]] = []
    notes_used: List[str] = []
    for key in parsed.get("citations", []) or []:
        candidate = index_map.get(str(key))
        if candidate is None:
            warnings.append(f"model cited unknown key {key}")
            continue
        citations.append(
            {
                "key": str(key),
                "note_id": candidate.get("note_id", ""),
                "path": candidate.get("path", ""),
                "title": candidate.get("title", ""),
                "heading": candidate.get("heading", ""),
                "excerpt": candidate.get("excerpt", ""),
            }
        )
        path = candidate.get("path", "")
        if path and path not in notes_used:
            notes_used.append(path)

    confidence = str(parsed.get("confidence", "") or "").strip().lower()
    return {
        "question": question,
        "answer": parsed.get("answer", ""),
        "confidence": confidence,
        "abstained": bool(parsed.get("abstained", False)),
        "citations": citations,
        "notes_used": notes_used,
        "warnings": warnings,
    }
