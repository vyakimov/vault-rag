"""Streamlit interface for searching vault notes."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict

import streamlit as st
from streamlit_models import get_store_and_searcher

from vault_spider.config import DEFAULT_SEARCH_PARAMS
from vault_spider.retrieval.evidence import build_retrieval_output

st.set_page_config(
    page_title="Vault Spider",
    page_icon="🗂️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .scores-row { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.25rem; }
    .score-badge {
        background-color: #eef1f6;
        color: #222;
        padding: 0.15rem 0.5rem;
        border-radius: 0.4rem;
        font-size: 0.75rem;
        font-weight: 600;
        border: 1px solid #d8dbe3;
    }
    .score-badge.primary { background-color: #e6f4ff; border-color: #b6dcff; }
    .score-badge.keyword { background-color: #e8f5e9; border-color: #a5d6a7; }
    .score-badge.semantic { background-color: #f3e5f5; border-color: #ce93d8; }
    .score-badge.fused { background-color: #fff3e0; border-color: #ffcc80; }
    .score-badge.reranked { background-color: #fff4e6; border-color: #ffd699; }
    .score-badge.recency { background-color: #e0f2f1; border-color: #80cbc4; }
    </style>
    """,
    unsafe_allow_html=True,
)


def display_candidate(index: int, candidate: Dict[str, object]):
    title = candidate.get("title", "(untitled)")
    path = candidate.get("path", "")
    heading = candidate.get("heading", "")
    scores = candidate.get("scores", {})
    final_score = float(scores.get("final", 0.0))

    label = f"📄 {index + 1}. {title}"
    if heading:
        label += f" › {heading}"
    with st.expander(f"{label} · {final_score:.4f}", expanded=(index < 3)):
        footer_parts = [path]
        if candidate.get("line_start"):
            footer_parts.append(f"L{candidate['line_start']}–{candidate['line_end']}")
        footer = " • ".join(part for part in footer_parts if part)
        if footer:
            st.caption(footer)

        chips = []
        for key, label_text, css_class in [
            ("bm25", "Keyword", "keyword"),
            ("semantic", "Semantic", "semantic"),
            ("fused", "Fused", "fused"),
            ("reranker", "Reranker", "reranked"),
            ("final", "Final", "primary"),
        ]:
            value = scores.get(key)
            if value is None:
                continue
            chips.append(
                f"<span class='score-badge {css_class}'>{label_text}: {float(value):.2f}</span>"
            )
        st.markdown(
            f"<div class='scores-row'>{''.join(chips)}</div>", unsafe_allow_html=True
        )
        if candidate.get("why"):
            st.caption(f"Why: {candidate['why']}")

        st.markdown(candidate.get("excerpt", ""))


def main():
    st.title("🗂️ Vault Spider")
    st.caption("Retrieve Markdown notes using BM25, embeddings, reranking, and recency.")

    store, searcher, init_error = get_store_and_searcher()
    if init_error:
        st.error(init_error)
        st.stop()

    assert store is not None
    assert searcher is not None

    if "last_results" not in st.session_state:
        st.session_state.last_results = None
    if "llm_response" not in st.session_state:
        st.session_state.llm_response = None

    with st.sidebar:
        st.header("⚙️ Configuration")
        stats = store.get_collection_stats()
        st.metric("Total Notes", int(stats.get("total_documents", 0)))
        st.caption(f"Section entries: {int(stats.get('section_entries', 0))}")
        st.caption(f"Embedding model: `{stats.get('embedding_model', 'unknown')}`")

        st.divider()
        st.subheader("Retrieval")
        mode = st.radio("Mode", ["fast", "thorough"], horizontal=True)
        granularity = st.radio(
            "Granularity", ["document", "section", "mixed"], horizontal=True
        )
        n_results = st.number_input(
            "Displayed Results",
            min_value=1,
            max_value=100,
            value=DEFAULT_SEARCH_PARAMS.n_results,
            step=1,
        )

    col1, col2 = st.columns([5, 1])
    with col1:
        query = st.text_input(
            "Retrieval query",
            placeholder="Where did I write about OpenClaw VPS migration?",
            key="search_query",
        )
    with col2:
        st.markdown(
            '<p style="margin-bottom: 0.25rem; font-size: 0.875rem;">&nbsp;</p>',
            unsafe_allow_html=True,
        )
        search_button = st.button("🔍 Retrieve", type="secondary", use_container_width=True)

    if search_button and query:
        st.session_state.llm_response = None
        with st.spinner("Retrieving vault notes..."):
            result = searcher.hybrid_search(
                query=query,
                mode=mode,
                granularity=granularity,
                n_results=int(n_results),
            )
            st.session_state.last_results = build_retrieval_output(
                query, mode, granularity, result.rows
            )

    if st.session_state.last_results is None:
        return

    results = st.session_state.last_results
    candidates = results.get("candidates", [])
    st.success(
        f"Retrieved {len(candidates)} candidates "
        f"({results.get('mode')} · {results.get('granularity')})."
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🗂️ Browse Notes", type="primary", use_container_width=True):
            st.switch_page("./streamlit_db.py")
    with col2:
        if st.button("🤖 Synthesize With OpenRouter", type="primary", use_container_width=True):
            st.switch_page("./streamlit_llm.py")

    for index, candidate in enumerate(candidates):
        display_candidate(index, candidate)

    st.divider()
    export_content = json.dumps(results, indent=2)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.download_button(
            label="Download JSON",
            data=export_content,
            file_name=f"vault_search_{timestamp}.json",
            mime="application/json",
            use_container_width=True,
        )
    with col2:
        if st.button("Save To Server", use_container_width=True):
            results_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "search_results",
            )
            os.makedirs(results_dir, exist_ok=True)
            save_path = os.path.join(results_dir, f"vault_search_{timestamp}.json")
            with open(save_path, "w", encoding="utf-8") as handle:
                handle.write(export_content)
            st.success(f"Saved to {save_path}")
    with col3:
        if st.button("Clear Cache & Reload", use_container_width=True):
            st.cache_resource.clear()
            st.rerun()


if __name__ == "__main__":
    main()
