from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional, Tuple

import streamlit as st

try:
    from scripts.database_reader import DatabaseReader
    from scripts.streamlit_models import get_openrouter_client
except ImportError:
    from database_reader import DatabaseReader
    from streamlit_models import get_openrouter_client


client = get_openrouter_client()

st.markdown("# Answer")


if "last_results" not in st.session_state:
    st.session_state["last_results"] = None
if "llm_response" not in st.session_state:
    st.session_state["llm_response"] = None


def transform_citations_to_links(answer: str, index_dict: Dict[str, str], base_url: str = "streamlit_db") -> str:
    def replace_group(match):
        citation_keys = [part.strip() for part in match.group(1).split(",")]
        links = []
        for citation_key in citation_keys:
            doc_id = index_dict.get(citation_key)
            if doc_id:
                links.append(f"[{citation_key}]({base_url}?doc_id={doc_id})")
            else:
                links.append(citation_key)
        return f"[{', '.join(links)}]"

    return re.sub(r"\[([A-Z]\d+(?:,\s*[A-Z]\d+)*)\]", replace_group, answer)


def parse_llm_json(response: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        start = response.find("{")
        end = response.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(response[start:end])
            except json.JSONDecodeError:
                return None
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
    index_dict = {}
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


@st.dialog("Note Details", width="large")
def show_document_dialog(doc_id: str, citation_key: str):
    reader = DatabaseReader()
    fetched = reader.collection.get(ids=[doc_id], include=["metadatas", "documents"])
    if not fetched or not fetched.get("ids"):
        st.warning("Document not found.")
        return
    metadata = (fetched.get("metadatas") or [{}])[0]
    document = (fetched.get("documents") or [""])[0]
    st.markdown(f"### {citation_key}: {metadata.get('title', '(untitled)')}")
    footer = " • ".join(
        part
        for part in [metadata.get("path", ""), metadata.get("date", ""), metadata.get("tags", "")]
        if part
    )
    if footer:
        st.caption(footer)
    st.markdown(document)


def write_response(response: str, index_dict: Dict[str, str]):
    parsed = parse_llm_json(response)
    if not parsed:
        st.error("Failed to parse response JSON")
        st.code(response)
        return

    answer = transform_citations_to_links(parsed.get("answer", ""), index_dict)
    citations = parsed.get("citations", [])
    confidence = parsed.get("confidence", "Unknown")

    st.markdown(answer)
    st.markdown(f"**Confidence:** {confidence}")

    if citations:
        st.markdown("**Citations**")
        cols = st.columns(min(len(citations), 6) + 1)
        for index, citation in enumerate(citations[:6]):
            doc_id = index_dict.get(citation)
            with cols[index]:
                if doc_id and st.button(f"📄 {citation}", key=f"cite_{citation}"):
                    show_document_dialog(doc_id, citation)


if st.session_state["last_results"] is None:
    st.write("Run a search first.")
else:
    results = st.session_state["last_results"]
    query = results["debug_info"]["query"]
    context, index_dict = build_context(results)

    with st.chat_message("user"):
        st.write(query)

    col1, col2 = st.columns([1, 5])
    with col1:
        if st.button("🔄 Regenerate"):
            st.session_state["llm_response"] = None
            st.rerun()

    st.caption(f"Using {len(index_dict)} notes as answer context.")

    if st.session_state["llm_response"] is None:
        system_prompt, user_prompt = generate_prompts(query, context)
        with st.spinner("Generating answer..."):
            st.session_state["llm_response"] = client.chat(system_prompt, user_prompt)

    with st.chat_message("assistant"):
        write_response(st.session_state["llm_response"], index_dict)
