"""Cached providers and search resources for Streamlit pages."""

from __future__ import annotations

from typing import Optional, Tuple

import streamlit as st

try:
    from scripts.database_builder import DatabaseBuilder
    from scripts.openrouter_client import OpenRouterClient
    from scripts.searcher import Searcher
except ImportError:
    from database_builder import DatabaseBuilder
    from openrouter_client import OpenRouterClient
    from searcher import Searcher


@st.cache_resource
def get_openrouter_client() -> OpenRouterClient:
    return OpenRouterClient.from_env()


@st.cache_resource
def get_database_and_searcher() -> Tuple[Optional[DatabaseBuilder], Optional[Searcher], Optional[str]]:
    try:
        provider = get_openrouter_client()
        builder = DatabaseBuilder(provider=provider)
        if builder.collection.count() == 0:
            return (
                None,
                None,
                "No notes found in the database. Please run `uv run scripts/build_database.py` first.",
            )

        searcher = Searcher(
            collection=builder.collection,
            documents=builder.documents,
            document_ids=builder.document_ids,
            metadatas=builder.metadatas,
            bm25=builder.bm25,
            tokenized_documents=builder.tokenized_documents,
            provider=provider,
        )
        return builder, searcher, None
    except Exception as exc:
        return None, None, f"Error initializing database: {exc}"
