# Assistant.md

This repository is **Vault RAG**, a retrieval system for Markdown notes stored in `input/Vault 14`.

## Project Overview

The project indexes `.md` files from the vault into ChromaDB, builds a BM25 index, and exposes hybrid retrieval with optional OpenRouter-based reranking and answer generation.

## Key Commands

- `uv run scripts/build_database.py`
  - Recursively ingests Markdown notes from `input/Vault 14`
  - Parses frontmatter, derives note metadata, generates embeddings through OpenRouter, and stores documents in ChromaDB
- `uv run scripts/ask_question.py "your question" [-o output.txt]`
  - Runs hybrid search against the indexed notes
  - Prints note titles, paths, and score breakdowns
- `uv run streamlit run scripts/streamlit_app.py`
  - Starts the Streamlit UI for search, note browsing, and answer generation
- `uv run scripts/tune_parameters.py --k1 ... --b ... --sw ... -q "query1" ...`
  - Lightweight search-parameter tuning helper

## Environment

Required environment variables:

- `OPENROUTER_API_KEY`
- `OPENROUTER_EMBEDDING_MODEL`
- `OPENROUTER_CHAT_MODEL`

Optional:

- `OPENROUTER_RERANK_MODEL`
- `OPENROUTER_BASE_URL`
- `OPENROUTER_HTTP_REFERER`
- `OPENROUTER_APP_TITLE`

Install dependencies with `uv sync`.

## Architecture

### Core Components

1. `scripts/vault_ingestion.py`
   - Reads Markdown notes from the vault
   - Parses YAML frontmatter when present
   - Resolves note dates using frontmatter, filename, then filesystem mtime

2. `scripts/openrouter_client.py`
   - Handles embeddings, reranking, and chat completions through OpenRouter
   - Uses env-configured model identifiers

3. `scripts/database_builder.py`
   - Manages the ChromaDB collection lifecycle
   - Stores note text plus scalar metadata
   - Rebuilds BM25 in memory

4. `scripts/searcher.py`
   - Combines Chroma vector search and BM25 keyword scores
   - Supports RRF and z-score/sigmoid fusion
   - Applies optional OpenRouter reranking and recency boosting

5. `scripts/streamlit_*.py`
   - Search UI
   - Note browser
   - OpenRouter-backed answer page

## Paths & Persistence

- ChromaDB directory: `./chroma_db/`
- Default note source: `./input/Vault 14/`
- Streamlit entrypoint: `scripts/streamlit_app.py`

## Development Notes

- One Markdown file is indexed as one document
- Metadata stored with each note includes title, path, folder, tags, and resolved date
- The search stack depends on explicit OpenRouter calls; no local model loading remains in the repository
