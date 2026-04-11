from __future__ import annotations

import os
import sys
from typing import Dict, Optional

import streamlit as st

try:
    from scripts.database_reader import DatabaseReader
except ImportError:
    try:
        from .scripts.database_reader import DatabaseReader  # pyright: ignore
    except ImportError:
        script_dir = os.path.join(os.getcwd(), "scripts")
        sys.path.insert(0, script_dir)
        from database_reader import DatabaseReader  # type: ignore


@st.cache_resource
def get_reader() -> Optional[DatabaseReader]:
    try:
        return DatabaseReader()
    except Exception as exc:
        st.error(f"Failed to initialize database: {exc}")
        return None


def show_document(reader: DatabaseReader, doc_id: str, scores: Optional[Dict[str, str]] = None, index: int = 0):
    scores = scores or {}
    fetched = reader.collection.get(ids=[doc_id], include=["metadatas", "documents"])
    if not fetched or not fetched.get("ids"):
        st.warning("Document not found.")
        return

    metadata = (fetched.get("metadatas") or [{}])[0]
    document = (fetched.get("documents") or [""])[0]
    title = metadata.get("title", "(untitled)")
    path = metadata.get("path", "")
    date = metadata.get("date", "")
    tags = metadata.get("tags", "")

    with st.expander(f"📄 {title}", expanded=(index < 3)):
        footer = " • ".join(part for part in [path, date, tags] if part)
        if footer:
            st.caption(footer)

        if scores:
            chips = []
            for label, key in [
                ("Keyword", "keyword"),
                ("Semantic", "semantic"),
                ("Fused", "fused"),
                ("Reranked", "reranked"),
                ("Boosted", "boosted"),
            ]:
                if scores.get(key) is not None:
                    chips.append(f"`{label}: {scores[key]}`")
            if chips:
                st.write(" ".join(chips))

        st.markdown(document)


def main():
    st.title("🗂️ Note Browser")
    reader = get_reader()
    if reader is None or reader.collection is None:
        st.stop()

    with st.sidebar:
        st.subheader("Lookup By ID")
        input_id = st.text_input("Document ID", value="")
        go = st.button("Load", use_container_width=True)
        st.divider()
        stats = reader.get_collection_stats()
        st.metric("Total Notes", int(stats.get("total_documents", 0)))
        st.caption(
            f"Folders: {stats.get('unique_folders', 0)} · Tags: {stats.get('unique_tags', 0)}"
        )

    if go and input_id:
        st.query_params.clear()
        st.query_params["doc_id"] = input_id

    qp = st.query_params
    requested_ids = qp.get_all("doc_id")
    if requested_ids:
        for index, doc_id in enumerate(requested_ids):
            show_document(reader, doc_id, index=index)
            st.divider()
        return

    if "last_results" in st.session_state and st.session_state["last_results"]:
        results = st.session_state["last_results"]
        st.info(f"Showing notes for query: {results['debug_info']['query']}")
        for index, doc_id in enumerate(results["ids"]):
            scores: Dict[str, str] = {
                "keyword": f"{results['keyword_scores'][index]:.3f}",
                "semantic": f"{results['semantic_scores'][index]:.3f}",
                "fused": f"{results['fused_scores'][index]:.3f}",
                "reranked": f"{results['reranked_scores'][index]:.3f}",
                "boosted": f"{results['boosted_scores'][index]:.3f}",
            }
            show_document(reader, doc_id, scores, index=index)
            st.divider()
        return

    st.write("No note selected. Showing a small sample.")
    sample = reader.collection.get(limit=10, include=["metadatas", "documents"])
    for index, doc_id in enumerate(sample.get("ids", [])):
        show_document(reader, doc_id, index=index)
        st.divider()


if __name__ == "__main__":
    main()
