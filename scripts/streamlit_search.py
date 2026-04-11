"""Streamlit interface for searching vault notes."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Dict

import streamlit as st
from rank_bm25 import BM25Okapi

try:
    from scripts.config import BM25_CONFIG, SEARCH_CONFIG
    from scripts.streamlit_models import get_database_and_searcher
except ImportError:
    try:
        from .scripts.config import BM25_CONFIG, SEARCH_CONFIG  # pyright: ignore
        from .scripts.streamlit_models import get_database_and_searcher  # pyright: ignore
    except ImportError:
        script_dir = os.path.join(os.getcwd(), "scripts")
        sys.path.insert(0, script_dir)
        from config import BM25_CONFIG, SEARCH_CONFIG  # pyright: ignore
        from streamlit_models import get_database_and_searcher  # pyright: ignore


st.set_page_config(
    page_title="Vault RAG",
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


def create_bm25_index(builder, k1: float, b: float):
    return BM25Okapi(builder.tokenized_documents, k1=k1, b=b)


def display_result(index: int, document: str, metadata: Dict[str, str], scores: Dict[str, float]):
    title = metadata.get("title", "(untitled)")
    path = metadata.get("path", "")
    date = metadata.get("date", "")
    tags = metadata.get("tags", "")
    final_score = scores.get("combined", 0.0)

    with st.expander(f"📄 {index + 1}. {title} · {final_score:.4f}", expanded=(index < 3)):
        footer = " • ".join(part for part in [path, date, tags] if part)
        if footer:
            st.caption(footer)

        chips = []
        for key, label, css_class in [
            ("keyword", "Keyword", "keyword"),
            ("semantic", "Semantic", "semantic"),
            ("fused", "Fused", "fused"),
            ("reranked", "Reranked", "reranked"),
            ("recency", "Recency", "recency"),
        ]:
            if key in scores:
                chips.append(
                    f"<span class='score-badge {css_class}'>{label}: {scores[key]:.2f}</span>"
                )
        chips.append(
            f"<span class='score-badge primary'>Final: {final_score:.2f}</span>"
        )
        st.markdown(
            f"<div class='scores-row'>{''.join(chips)}</div>", unsafe_allow_html=True
        )

        st.markdown(document)
        relevant = st.checkbox(
            "Use this note for answer generation",
            key=f"relevant_{index}",
            value=st.session_state.last_results["relevant"][index],
        )
        st.session_state.last_results["relevant"][index] = relevant


def main():
    st.title("🗂️ Vault RAG")
    st.caption("Search Markdown notes from `input/Vault 14` using BM25, embeddings, reranking, and recency.")

    builder, searcher, init_error = get_database_and_searcher()
    if init_error:
        st.error(init_error)
        st.stop()

    assert builder is not None
    assert searcher is not None

    if "last_results" not in st.session_state:
        st.session_state.last_results = None
    if "llm_response" not in st.session_state:
        st.session_state.llm_response = None

    with st.sidebar:
        st.header("⚙️ Configuration")
        stats = builder.get_collection_stats()
        st.metric("Total Notes", int(stats.get("total_documents", 0)))
        st.caption(f"Embedding model: `{stats.get('embedding_model', 'unknown')}`")

        st.divider()
        st.subheader("BM25")
        k1 = st.slider("k1", 0.0, 3.0, float(BM25_CONFIG.get("k1", 1.2)), 0.1)
        b = st.slider("b", 0.0, 1.0, float(BM25_CONFIG.get("b", 0.75)), 0.05)
        if k1 != BM25_CONFIG.get("k1", 1.2) or b != BM25_CONFIG.get("b", 0.75):
            if st.button("Update BM25 Index"):
                searcher.bm25 = create_bm25_index(builder, k1, b)
                st.success("BM25 index updated.")

        st.divider()
        st.subheader("Hybrid Search")
        semantic_weight = st.slider(
            "Semantic Weight",
            0.0,
            1.0,
            float(SEARCH_CONFIG.get("semantic_weight", 0.5)),
            0.05,
        )
        top_k = st.number_input(
            "Candidate Pool",
            min_value=1,
            max_value=1000,
            value=int(SEARCH_CONFIG.get("default_top_k", 150)),
            step=1,
        )
        n_results = st.number_input(
            "Displayed Results",
            min_value=1,
            max_value=100,
            value=int(SEARCH_CONFIG.get("n_results", 10)),
            step=1,
        )

        st.divider()
        st.subheader("Recency")
        use_recency = st.checkbox(
            "Enable Recency Boost",
            value=bool(SEARCH_CONFIG.get("recency_boost_enabled", True)),
        )
        recency_weight = st.slider(
            "Recency Weight",
            0.0,
            1.0,
            float(SEARCH_CONFIG.get("recency_weight", 0.2)),
            0.05,
        )
        recency_decay_days = st.number_input(
            "Recency Half-life (days)",
            min_value=1,
            max_value=3650,
            value=int(SEARCH_CONFIG.get("recency_decay_days", 365.0)),
            step=1,
        )

    col1, col2 = st.columns([5, 1])
    with col1:
        query = st.text_input(
            "Search query",
            placeholder="Where did I write about OpenClaw VPS migration?",
            key="search_query",
        )
    with col2:
        st.markdown(
            '<p style="margin-bottom: 0.25rem; font-size: 0.875rem;">&nbsp;</p>',
            unsafe_allow_html=True,
        )
        search_button = st.button("🔍 Search", type="secondary", use_container_width=True)

    if search_button and query:
        st.session_state.llm_response = None
        with st.spinner("Searching vault notes..."):
            results = searcher.hybrid_search(
                query=query,
                top_k=int(top_k),
                semantic_weight=semantic_weight,
                number_of_results=int(n_results),
                recency_boost_enabled=use_recency,
                recency_weight=recency_weight,
                recency_decay_days=float(recency_decay_days),
            )
            st.session_state.last_results = results

    if st.session_state.last_results is None:
        return

    results = st.session_state.last_results
    strategy = results.get("debug_info", {}).get("combine_strategy", "rrf")
    st.success(f"Retrieved {len(results['documents'])} notes.")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🗂️ Browse Notes", type="primary", use_container_width=True):
            st.switch_page("./streamlit_db.py")
    with col2:
        if st.button("🤖 Answer With OpenRouter", type="primary", use_container_width=True):
            st.switch_page("./streamlit_llm.py")

    for index, document in enumerate(results["documents"]):
        scores: Dict[str, float] = {
            "combined": results["boosted_scores"][index],
            "keyword": results["keyword_scores"][index],
            "semantic": results["semantic_scores"][index],
            "fused": results["fused_scores"][index],
        }
        if index < len(results.get("reranked_scores", [])):
            scores["reranked"] = results["reranked_scores"][index]
        if index < len(results.get("recency_boost_factor", [])):
            scores["recency"] = results["recency_boost_factor"][index]
        display_result(index, document, results["metadatas"][index], scores)

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
