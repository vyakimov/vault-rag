from __future__ import annotations

from typing import Dict, Optional

import streamlit as st

from vault_spider import settings
from vault_spider.index.reader import DatabaseReader


@st.cache_resource
def get_reader() -> Optional[DatabaseReader]:
    try:
        return DatabaseReader(settings.chroma_path())
    except Exception as exc:  # noqa: BLE001
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
                ("Keyword", "bm25"),
                ("Semantic", "semantic"),
                ("Fused", "fused"),
                ("Reranker", "reranker"),
                ("Final", "final"),
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
        input_id = st.text_input("Entry ID", value="")
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
        st.info(f"Showing notes for query: {results.get('query', '')}")
        seen = set()
        index = 0
        for candidate in results.get("candidates", []):
            note_id = candidate.get("note_id")
            if note_id in seen:
                continue
            seen.add(note_id)
            scores = {
                key: f"{value:.3f}"
                for key, value in candidate.get("scores", {}).items()
                if value is not None
            }
            show_document(reader, f"{note_id}::doc", scores, index=index)
            st.divider()
            index += 1
        return

    st.write("No note selected. Showing a small sample.")
    sample = reader.collection.get(
        where={"granularity": "document"},
        limit=10,
        include=["metadatas", "documents"],
    )
    for index, doc_id in enumerate(sample.get("ids", [])):
        show_document(reader, doc_id, index=index)
        st.divider()


if __name__ == "__main__":
    main()
