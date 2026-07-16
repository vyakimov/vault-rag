from __future__ import annotations

import re
from typing import Dict

import streamlit as st
from streamlit_models import get_openrouter_client

from vault_spider import settings
from vault_spider.index.reader import DatabaseReader
from vault_spider.synthesis.answer import synthesize

st.markdown("# Synthesize")


if "last_results" not in st.session_state:
    st.session_state["last_results"] = None
if "llm_response" not in st.session_state:
    st.session_state["llm_response"] = None


def transform_citations_to_links(
    answer: str, key_to_docid: Dict[str, str], base_url: str = "streamlit_db"
) -> str:
    def replace_group(match):
        citation_keys = [part.strip() for part in match.group(1).split(",")]
        links = []
        for citation_key in citation_keys:
            doc_id = key_to_docid.get(citation_key)
            if doc_id:
                links.append(f"[{citation_key}]({base_url}?doc_id={doc_id})")
            else:
                links.append(citation_key)
        return f"[{', '.join(links)}]"

    return re.sub(r"\[([A-Z]\d+(?:,\s*[A-Z]\d+)*)\]", replace_group, answer)


@st.dialog("Note Details", width="large")
def show_document_dialog(doc_id: str, citation_key: str):
    reader = DatabaseReader(settings.chroma_path())
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


def write_response(synth: Dict[str, object]):
    citations = synth.get("citations", []) or []
    key_to_docid = {c["key"]: f"{c['note_id']}::doc" for c in citations}

    answer = transform_citations_to_links(str(synth.get("answer", "")), key_to_docid)
    st.markdown(answer or "_(no answer)_")
    st.markdown(f"**Confidence:** {synth.get('confidence', 'unknown')}")
    if synth.get("abstained"):
        st.warning("The model abstained: the notes did not contain enough information.")
    for warning in synth.get("warnings", []) or []:
        st.caption(f"⚠️ {warning}")

    if citations:
        st.markdown("**Citations**")
        cols = st.columns(min(len(citations), 6))
        for index, citation in enumerate(citations[:6]):
            doc_id = key_to_docid.get(citation["key"])
            with cols[index]:
                if doc_id and st.button(f"📄 {citation['key']}", key=f"cite_{citation['key']}"):
                    show_document_dialog(doc_id, citation["key"])


if st.session_state["last_results"] is None:
    st.write("Run a retrieval first.")
else:
    retrieval_output = st.session_state["last_results"]
    query = retrieval_output.get("query", "")
    try:
        client = get_openrouter_client()
    except Exception as exc:
        st.error(f"OpenRouter is not configured: {exc}")
        st.stop()

    with st.chat_message("user"):
        st.write(query)

    col1, col2 = st.columns([1, 5])
    with col1:
        if st.button("🔄 Regenerate"):
            st.session_state["llm_response"] = None
            st.rerun()

    st.caption(f"Using up to {len(retrieval_output.get('candidates', []))} candidates for synthesis.")

    if st.session_state["llm_response"] is None:
        if st.button("🤖 Synthesize", type="primary"):
            with st.spinner("Synthesizing..."):
                st.session_state["llm_response"] = synthesize(
                    client, retrieval_output, question=query, max_tokens=4096
                )
            st.rerun()
    else:
        with st.chat_message("assistant"):
            write_response(st.session_state["llm_response"])
