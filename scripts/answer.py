"""Pure helpers for turning retrieval results into an LLM-synthesized answer.

Kept free of Streamlit imports so both the CLI (``ask_question.py``) and the
Streamlit UI can share the same prompt, context, and JSON-parsing logic.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    from scripts.openrouter_client import OpenRouterClient
except ImportError:
    from openrouter_client import OpenRouterClient


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


def generate_prompts(query: str, context: str) -> Tuple[str, str]:
    system_prompt = """You are a retrieval-grounded assistant.
Use only the note excerpts provided in <CONTEXT>.
Every factual claim must cite one or more note ids like [S0] or [S0, S1].
If the notes do not contain enough information, say that clearly.
Return JSON with this exact shape:
{
  "answer": "<text>",
  "citations": ["S0"],
  "confidence": "High|Medium|Low"
}
"""
    user_prompt = f"""<QUERY>
{query}
</QUERY>
<CONTEXT>
{context}
</CONTEXT>
Answer using only the context above."""
    return system_prompt, user_prompt


def build_context(results, hard_cutoff: int = 8) -> Tuple[str, Dict[str, str]]:
    context_parts = []
    index_dict: Dict[str, str] = {}
    for index, (doc_id, document, metadata, relevant) in enumerate(
        zip(
            results["ids"],
            results["documents"],
            results["metadatas"],
            results["relevant"],
        )
    ):
        if not relevant:
            continue
        citation_key = f"S{len(index_dict)}"
        context_parts.append(
            "\n".join(
                [
                    f"<{citation_key} score={results['boosted_scores'][index]:.4f}>",
                    f"Title: {metadata.get('title', '(untitled)')}",
                    f"Path: {metadata.get('path', '')}",
                    document,
                    f"</{citation_key}>",
                ]
            )
        )
        index_dict[citation_key] = doc_id
        if len(index_dict) >= hard_cutoff:
            break
    return "\n\n".join(context_parts), index_dict


def synthesize_answer(
    client: OpenRouterClient,
    results,
    hard_cutoff: int = 8,
    max_tokens: int = 4096,
) -> Tuple[Optional[Dict[str, Any]], str, Dict[str, str]]:
    """Run the full synthesis pipeline and return (parsed, raw, index_dict)."""
    context, index_dict = build_context(results, hard_cutoff=hard_cutoff)
    query = results["debug_info"]["query"]
    system_prompt, user_prompt = generate_prompts(query, context)
    raw = client.chat(system_prompt, user_prompt, max_tokens=max_tokens)
    parsed = parse_llm_json(raw)
    return parsed, raw, index_dict
