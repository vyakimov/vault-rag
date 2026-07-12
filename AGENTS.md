# Assistant.md

This repository is **Vault RAG**, a retrieval system for Markdown notes stored in `input/Vault 14`.

## Project Overview

The project indexes `.md` files from the vault into ChromaDB, builds BM25 indexes, and exposes
hybrid retrieval and answer synthesis with stable JSON contracts. It is organized as the
`vault_rag` Python package with a JSON-only CLI (`vault-rag`) plus a Streamlit UI.

One note is indexed as **one `document`-granularity entry plus N `section` entries** (section
splitting is deterministic, heading-based). Both live in a single Chroma collection, distinguished
by the `granularity` metadata field.

## Key Commands

- `uv run vault-rag schema`
  - Prints the machine-readable command + contract schema (`version: 1`).
- `uv run vault-rag sync --root "./input/Vault 14" [--reset]`
  - Incremental sync: adds new notes, re-embeds changed or moved notes, deletes removed notes.
    Notes sharing a duplicate frontmatter `id` are skipped after the first and reported in
    the result's `warnings`.
  - `--reset` rebuilds the collection from scratch (needed once after an entry-shape change).
- `uv run vault-rag retrieve --query "..." [--mode fast|thorough] [--granularity document|section|mixed] [-n 10]`
  - Returns the retrieval output contract (candidates with score breakdown). Defaults: `fast`, `document`.
    `mixed` searches the section pool with a 3-sections-per-note cap (it does not mix in document entries).
  - `fast` skips reranking; `thorough` reranks the top candidates.
- `uv run vault-rag synthesize --query "..." [--mode thorough] [--granularity mixed] [--retrieval file.json] [--n-context 8] [--save --root <dir> [--save-dir Distilled]]`
  - Retrieves (defaults `thorough`/`mixed`) then synthesizes a cited answer. `--retrieval` reuses a
    prior `retrieve` envelope/contract and skips retrieval. Abstains when the notes lack the answer.
  - `--save` persists a high-quality answer as a create-only **distilled note** (`type: distilled`)
    under `<root>/<save-dir>`. Skips (with a warning) when the answer abstained, is low-confidence,
    has no citations, or the target exists. Distilled notes are regenerable pointers to their
    sources — raw notes always win on conflict. Run `vault-rag sync` afterward to index it.
- `uv run vault-rag lint --root <dir> [--format json|text]`
  - Read-only corpus health report (no LLM, no writes, no index needed): missing frontmatter
    fields, invalid/naive timestamps, duplicate ids, broken wikilinks, orphans, stale distilled notes.
- `uv run vault-rag enrich --root <dir> (--note <path> | --stdin) [--intent ...] [--source-type transcript|web|pdf|manual] [--source-url ...] [--title ...]`
  - App-agnostic **enrichment planner**: retrieves a note's neighborhood and proposes a title,
    frontmatter patch (`type`/`aliases`/`source_type`/`source_url` only), inline links, related
    candidates, and placement — as JSON. It **never mutates** files or the index; applying a plan
    is obsctl's job. Only links to retrieved neighbors are proposed; the LLM output is validated in
    code (confidence gating, anchor resolution, existing-type/link guards).
- `uv run streamlit run scripts/streamlit_app.py`
  - Streamlit UI: Retrieve (mode + granularity selectors), Synthesize, Notes browser.
- `uv run pytest`
  - Network-free test suite (uses a fake provider; no API key required).

All CLI output is a single JSON envelope on stdout: `{"ok": bool, "action", "result", "meta"}` on
success, `{"ok": false, "action", "error": {"type", "message", "details"}}` on failure (exit 1).
Error types: `invalid_arguments`, `index_empty`, `provider_error`, `not_found`, `internal_error`.

## Environment

Required environment variables (loaded from `.env` via `python-dotenv`):

- `OPENROUTER_API_KEY`
- `OPENROUTER_EMBEDDING_MODEL`
- `OPENROUTER_CHAT_MODEL`

Optional:

- `OPENROUTER_RERANK_MODEL` (enables reranking in `thorough` mode)
- `OPENROUTER_BASE_URL`
- `OPENROUTER_HTTP_REFERER`
- `OPENROUTER_APP_TITLE`

Install dependencies with `uv sync`.

## Architecture

The `vault_rag` package is layered:

1. `vault_rag/corpus/`
   - `frontmatter.py` — YAML frontmatter split, tag normalization, datetime coercion.
   - `identity.py` — note id resolution (frontmatter `id`/ULID, else path hash).
   - `loader.py` — `load_notes(root)` → `Note` dataclasses (skips `#ignore`/`#secret` in the
     body or frontmatter `tags`, non-UTF-8 files, and `.trash/`, `.obsidian/`, `Templates/`).
   - `chunker.py` — deterministic `split_sections` plus `document_text` / `section_text`.

2. `vault_rag/index/`
   - `store.py` — `IndexStore` with `sync(root, reset)`; maintains the Chroma collection and two
     in-memory BM25 indexes (document + section), diffing by `note_id` + `content_hash`.
   - `reader.py` — read-only Chroma access for the Notes UI.

3. `vault_rag/retrieval/`
   - `fusion.py` — pure RRF / z-score-sigmoid / min-max fusion.
   - `searcher.py` — `Searcher.hybrid_search` (embeddings + BM25 + fusion + optional rerank +
     recency); `fast`/`thorough` modes, `document`/`section`/`mixed` granularity.
   - `evidence.py` — builds the retrieval output contract (candidate objects + deterministic `why`).

4. `vault_rag/synthesis/`
   - `answer.py` — `synthesize()` turns a retrieval contract into a cited answer with abstention;
     robust JSON parsing / truncation repair.

5. `vault_rag/compounding/`
   - `distill.py` — `save_distilled_note()` for `synthesize --save`.
   - `lint.py` — `lint_vault()` read-only health checks.
   `vault_rag/enrich/planner.py` — `plan()` enrichment planner (read-only; proposes, never mutates).

6. `vault_rag/llm/openrouter.py` — embeddings, rerank, and chat via OpenRouter.

7. `vault_rag/cli.py` + `vault_rag/envelope.py` — the JSON CLI and envelope helpers.

8. `scripts/streamlit_*.py` — Streamlit pages that import from `vault_rag`.

`tools/backfill.py` — standalone one-time migration that adds `id`/`created`/`updated`
frontmatter to existing notes (dry-run by default; `--apply` to write; never touches bodies).
`uv run tools/backfill.py --root <dir> [--apply] [--report <path>]`. Timestamp policy defaults to
UTC `Z` (`TIMESTAMP_POLICY` in the file); Phase 0 may switch it to offset-aware local.

## Paths & Persistence

- ChromaDB directory: `./chroma_db/`
- Default note source: `./input/Vault 14/`
- Streamlit entrypoint: `scripts/streamlit_app.py`

## Development Notes

- Intra-package imports are absolute (`from vault_rag.corpus import loader`); no import fallbacks.
- Metadata stored per entry: `note_id`, `granularity`, `title`, `path`, `folder`, `tags`, `date`,
  `created`, `updated`, `note_type`, `content_hash`, `heading`, `line_start`, `line_end`, `source`.
- Retrieval/synthesis JSON contracts are stable; downstream tooling depends on them (see
  `vault-rag schema`).
- The LLM relevance judge was removed; abstention now lives in synthesis.
