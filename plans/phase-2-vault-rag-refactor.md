# Phase 2 — vault-rag refactor: retrieval/synthesis split, JSON contracts, judge removal

**Executor:** a coding agent working in `/Users/vy/Documents/Development/vault-rag`.
**Prerequisites:** none (independent of Phases 0/1). Env vars needed to run against the real index: `OPENROUTER_API_KEY`, `OPENROUTER_EMBEDDING_MODEL`, `OPENROUTER_CHAT_MODEL`, optional `OPENROUTER_RERANK_MODEL` (all in `.env`, loaded via `python-dotenv`). Tests must not need network.
**This is the largest phase. Work through the steps in order; each step ends in a runnable state.**

## Goal

Restructure the flat `scripts/` codebase into a `vault_rag` package with:
- retrieval and synthesis as separate modules with stable JSON contracts;
- `document` / `section` / `mixed` granularity;
- `fast` / `thorough` retrieval modes;
- **complete removal of the LLM judge** (user decision — not opt-in, gone; git history preserves it);
- a JSON-only CLI (`vault-rag`) using the bearctl envelope pattern;
- incremental sync (add/update/delete) replacing the interactive build script;
- a pytest suite.

## Step 0 — Baseline before touching anything

1. Run `git status` — start from a clean tree on a new branch: `git checkout -b phase2-refactor`.
2. Capture a golden-query baseline: for each of the first 10 lines of `input/questions.txt`, run the **current** `uv run scripts/ask_question.py "<q>" -o /tmp/golden-before-<i>.txt`. Keep these files; they are compared (manually, for sanity, not byte-equality) at the end. If the env/API is unavailable, note it and skip — the pytest suite is the hard gate; golden queries are a soft check.

## Target layout

```
vault_rag/
  __init__.py
  config.py            # SEARCH_CONFIG / BM25_CONFIG (judge keys removed)
  envelope.py          # success()/failure()/print_json — copied from bearctl pattern
  utils.py             # hash_string, normalize_no_punct, tokenize_for_bm25,
                       # DEFAULT_STOP_WORDS, count_tokens (drop PrettyPrinter → stays in scripts/ if needed)
  corpus/
    __init__.py
    frontmatter.py     # split_frontmatter, normalize_tags, coerce_datetime  (from scripts/vault_ingestion.py)
    loader.py          # load_markdown_notes → returns Note objects (see below)
    chunker.py         # NEW: section splitting
    identity.py        # NEW: note_id resolution
  index/
    __init__.py
    store.py           # IndexStore (from DatabaseBuilder) + sync()
    reader.py          # DatabaseReader (from scripts/database_reader.py, unchanged behavior)
  retrieval/
    __init__.py
    fusion.py          # reciprocal_rank_fusion, zscore_sigmoid_fusion, min_max_scale (from Searcher, as free functions)
    searcher.py        # Searcher.hybrid_search — slimmed (no judge)
    evidence.py        # evidence-object assembly per citation contract
  synthesis/
    __init__.py
    answer.py          # from scripts/answer.py, adapted to evidence JSON
  llm/
    __init__.py
    openrouter.py      # OpenRouterClient (from scripts/openrouter_client.py, judge methods deleted)
  cli.py               # argparse CLI: schema, sync, retrieve, synthesize
scripts/
  streamlit_app.py     # kept; pages rewired to import from vault_rag
  streamlit_search.py, streamlit_llm.py, streamlit_db.py, streamlit_models.py
tools/
  backfill.py          # Phase 1 artifact (may not exist yet — leave alone)
tests/
  conftest.py, test_frontmatter.py, test_chunker.py, test_identity.py,
  test_fusion.py, test_store_sync.py, test_evidence.py, test_synthesis.py, test_cli.py
```

Deleted (git preserves them): `scripts/ask_question.py`, `scripts/tune_parameters.py`, `scripts/build_database.py`, `scripts/vault_ingestion.py`, `scripts/searcher.py`, `scripts/answer.py`, `scripts/openrouter_client.py`, `scripts/database_builder.py`, `scripts/database_reader.py`, `scripts/config.py`, `scripts/utils.py` (contents absorbed into the package; keep `PrettyPrinter` only if a Streamlit page imports it — check first).

## Step 1 — Package scaffold + packaging

1. `mkdir` the tree above with empty `__init__.py` files.
2. `pyproject.toml` changes:
   ```toml
   [build-system]
   requires = ["hatchling"]
   build-backend = "hatchling.build"

   [tool.hatch.build.targets.wheel]
   packages = ["vault_rag"]

   [project.scripts]
   vault-rag = "vault_rag.cli:main"
   ```
   Add dependencies: `python-ulid`. Add `[dependency-groups] dev = ["pytest"]`.
3. `uv sync`, then verify `uv run vault-rag --help` once `cli.py` has a stub `main()`.
4. All intra-package imports are absolute (`from vault_rag.corpus import frontmatter`). **No try/except import fallbacks anywhere.**

## Step 2 — Corpus layer

### `corpus/frontmatter.py`
Move `split_frontmatter`, `normalize_tags`, `coerce_datetime` verbatim from `scripts/vault_ingestion.py`.

### `corpus/identity.py`
```python
ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

def resolve_note_id(frontmatter: dict, relative_path: str) -> str:
    """Frontmatter `id` (any non-empty scalar, stripped) if present, else hash_string(relative_path)."""
```
Also `is_ulid(value) -> bool`. The path-hash fallback keeps pre-backfill vaults working; after Phase 1 all notes should hit the frontmatter branch.

### `corpus/loader.py`
Rework `load_markdown_notes` around a dataclass:

```python
@dataclass
class Note:
    note_id: str
    path: str            # vault-relative posix path
    title: str
    tags: list[str]
    created: str | None  # from frontmatter `created`, ISO string or None
    updated: str | None  # from frontmatter `updated`
    date: str            # resolved display/recency date: created > date > filename-date > mtime (existing resolve_note_date logic)
    note_type: str       # frontmatter `type` or ""
    body: str            # body without frontmatter
    raw_text: str        # full original file text
    content_hash: str    # sha256 hexdigest of raw_text

def load_notes(root: str) -> list[Note]
```
Keep: `#ignore` / `#secret` tag skipping (`has_ignore_tag`), `.md` rglob, sorted order. Also skip `.trash/`, `.obsidian/`, `Templates/`.

### `corpus/chunker.py`
```python
@dataclass
class Section:
    chunk_id: str        # f"{note_id}::s{index:03d}"
    note_id: str
    heading: str         # nearest heading text ("" for preamble)
    level: int           # heading level, 0 for preamble
    line_start: int      # 1-based, inclusive, within the BODY (not raw file)
    line_end: int        # 1-based, inclusive
    text: str

def split_sections(note: Note, max_chars: int = 6000, overlap_chars: int = 300) -> list[Section]
```
Algorithm (deterministic, no LLM):
1. Split `note.body` into lines. A heading line matches `^(#{1,6})\s+(.*)$` **outside fenced code blocks** (track ``` fences; toggle a flag).
2. Sections are the spans between headings of level 1–3. Headings of level 4–6 do not start new sections (they stay inside their parent). Content before the first heading is the "preamble" section (heading "", level 0) — only emitted if non-blank.
3. Each section's text includes its heading line.
4. If a section exceeds `max_chars`, split it into consecutive windows of `max_chars` with `overlap_chars` overlap, breaking at line boundaries (never mid-line). Split parts share the heading, get sequential chunk ids, and correct line ranges.
5. A note with no headings yields one section (the whole body, heading "").
6. Every body line belongs to exactly one section (except overlap duplication).

### Embedding text composition
Two helper functions (put in `chunker.py` or `loader.py`, either fine):
- `document_text(note)` — same as the current `build_document_text`: title + path + tags + date + body.
- `section_text(note, section)` — `f"# {note.title}\n\nSection: {section.heading or '(intro)'}\n\n{section.text}"`.

## Step 3 — Index layer (`index/store.py`)

Rename `DatabaseBuilder` → `IndexStore`, same constructor defaults (`chroma_db_path="chroma_db"`, `collection_name="vault_notes"`). Keep collection embedding-model guard. Changes:

**Chroma entries** (one collection, both granularities):
- id: `f"{note_id}::doc"` for documents, `chunk_id` for sections.
- document: `document_text` / `section_text` output.
- metadata (Chroma allows scalars only — str/int/float/bool):
  `note_id`, `granularity` ("document"|"section"), `title`, `path`, `folder`, `tags` (comma-joined), `date`, `created` (str, "" if None), `updated` (str, "" if None), `note_type`, `content_hash`, `heading` (sections; "" for docs), `line_start`/`line_end` (int; 0 for docs), `source`="vault_markdown".

**`sync(root: str, reset: bool = False) -> dict`** (replaces interactive build):
1. If `reset`: delete + recreate the collection.
2. `notes = load_notes(root)`; compute per-note desired entries.
3. Read existing: `collection.get(include=["metadatas"])`; group existing ids by `note_id`, note each group's `content_hash`.
4. Diff by `note_id`:
   - new → add all entries;
   - `content_hash` changed → `collection.delete(ids=[...old ids...])`, add fresh entries;
   - gone from disk → delete its ids;
   - unchanged → skip (no embedding calls).
5. Embed only added texts (`provider.embed_texts`, batch 32).
6. Rebuild in-memory state: two BM25 indexes (document-level and section-level) built from the collection contents, plus id→document / id→metadata maps split by granularity.
7. Return counts: `{"added_notes": n, "updated_notes": n, "deleted_notes": n, "unchanged": n, "total_entries": n}`.

Rehydrate-on-init behavior stays (load collection into memory, build both BM25 indexes).

## Step 4 — Retrieval layer

### `retrieval/fusion.py`
Move `min_max_scale`, `reciprocal_rank_fusion`, `zscore_sigmoid_fusion` from `Searcher` as module-level functions (they only used `self` for `min_max_scale` — make them pure). Keep behavior identical (tests lock this).

### `retrieval/searcher.py` — slimmed `Searcher`
Constructor takes the `IndexStore` (instead of six parallel lists) and a granularity: it pulls `collection`, per-granularity documents/ids/metadatas/bm25 from the store.

`hybrid_search(query, *, mode="fast", granularity="document", n_results=10, ...same tunables...) -> RetrievalResult`:

Pipeline (judge is **gone**):
1. Embed query; Chroma query filtered by `where={"granularity": granularity}` for document/section, or no filter for `mixed` — **no**: for `mixed`, query with `where={"granularity": "section"}` and group later. (`mixed` = section retrieval + document identity attached, per the Bear notes.)
2. BM25 scores from the matching granularity's index (for `mixed`: section index).
3. Fusion (RRF default) — unchanged math, including quoted-phrase boost and `must_include_terms` filtering (port them).
4. **mode="thorough" only:** rerank top `rerank_top_k` (default 30) via `provider.rerank`; on `OpenRouterError` fall back to fused order (as today). Apply the existing rank-based conversion (`rerank_use_ranks`). **mode="fast": skip rerank entirely**, even if a rerank model is configured.
5. Recency boost — keep existing formula; the date used is metadata `updated` if non-empty else `date`.
6. Take top `n_results`.

### Judge removal checklist (do these deletions explicitly)
- `llm/openrouter.py`: delete `judge_relevance`, `_judge_one`, `_score_to_grade`, `_JUDGE_SYSTEM_PROMPT`, `_JUDGE_USER_TEMPLATE`, the `judge_model` constructor param/attr and its `from_env` line (`OPENROUTER_JUDGE_MODEL`).
- `config.py`: delete `judge_enabled`, `judge_top_k`, `judge_votes`, `judge_filter_irrelevant`. Set `rerank_top_k: 30`.
- Searcher: delete everything between the rerank block and the recency block that touches `judge_*` columns, `relevance_score` becomes just the reranked/fused score.
- Streamlit search page: remove judge score badges/columns (grep `judge` in `scripts/streamlit_*.py`).
- Grep the whole repo for `judge` at the end — zero hits outside `plans/` and git history.

### `retrieval/evidence.py` — the citation contract
```python
def build_evidence(result_row, store, granularity) -> dict
def build_retrieval_output(query, mode, granularity, rows) -> dict
```
Retrieval output JSON (stable contract — downstream phases depend on it):
```json
{
  "query": "...",
  "mode": "fast",
  "granularity": "section",
  "candidates": [
    {
      "note_id": "01HSZ... or path-hash",
      "path": "Research/Rose/Vogquestue.md",
      "title": "Rose Vogquestue",
      "type": "",                          
      "heading": "Symptoms",
      "chunk_id": "01HSZ...::s003",
      "line_start": 42,
      "line_end": 68,
      "excerpt": "first 700 chars of the entry text",
      "scores": {"bm25": 12.4, "semantic": 0.81, "fused": 0.63, "reranker": 0.92, "final": 0.71},
      "why": "..."
    }
  ]
}
```
- Document-granularity candidates: `heading` "", `chunk_id` = `<note_id>::doc`, line range 0/0.
- `mixed`: candidates are sections, ordered by score, **at most 3 sections per note**; no separate document objects (the note identity is already on each section).
- `reranker` is `null` when mode=fast.
- `why` is deterministic (no LLM). Rules, first match wins:
  - reranker ran and its rank ≤ 3 → `"reranked into top {rank} for this query"`;
  - bm25 z-score > semantic z-score (within the candidate set) → `"strong keyword match"`;
  - semantic z-score > bm25 → `"strong semantic match"`;
  - else → `"combined keyword+semantic signal"`.

## Step 5 — Synthesis layer (`synthesis/answer.py`)

Keep `parse_llm_json` + `_try_repair_truncated_json` (proven). Changes:

- `build_context(retrieval_output, hard_cutoff=8)` consumes the retrieval JSON above (candidates list), not the old dict-of-lists. Context blocks keep the `<S0 ...>` tag format with title + path + text. Map `S{i}` → candidate.
- Prompt: keep the current system prompt, with the JSON contract extended to `{"answer", "citations": ["S0"], "confidence": "High|Medium|Low", "abstained": true|false}` and one added instruction line: `If the notes do not contain enough information to answer, set "abstained": true and say what is missing.`
- `synthesize(client, retrieval_output, question=None, hard_cutoff=8, max_tokens=4096) -> dict` returns the **synthesis output contract**:
```json
{
  "question": "...",
  "answer": "...",
  "confidence": "medium",
  "abstained": false,
  "citations": [
    {"key": "S0", "note_id": "...", "path": "...", "title": "...", "heading": "...", "excerpt": "..."}
  ],
  "notes_used": ["Research/Rose/Vogquestue.md"],
  "warnings": []
}
```
- Citations: resolve the LLM's `S*` keys back to the candidate objects; unknown keys → drop + warning `"model cited unknown key S9"`. `confidence` lower-cased. If the LLM output fails to parse even after repair → `{"answer": "", "abstained": true, "warnings": ["unparseable model output"], "raw": "<raw text>"}`.

## Step 6 — CLI (`cli.py` + `envelope.py`)

`envelope.py`: copy the bearctl pattern —
```python
def success(action, result=None, meta=None) -> dict   # {"ok": True, "action", "result", "meta"}
def failure(action, err_type, message, details=None) -> dict
def print_json(payload)                               # compact separators, sort_keys=True
```
Error types: `invalid_arguments`, `index_empty`, `provider_error`, `not_found`, `internal_error`.

Commands (all JSON to stdout, one envelope per invocation; errors → envelope with `ok: false` and exit code 1):

```bash
vault-rag schema                     # machine-readable command + contract description, "version": 1
vault-rag sync --root <dir> [--reset]
vault-rag retrieve --query "..." [--mode fast|thorough] [--granularity document|section|mixed] [-n 10]
vault-rag synthesize --query "..." [--mode thorough] [--granularity mixed] [--retrieval <file.json>] [--n-context 8]
```
- `retrieve` result = the retrieval output contract; `meta` carries `timing_ms` and the effective tunables (old `debug_info`).
- `synthesize`: with `--retrieval` it reads a prior `retrieve` envelope (accept either the raw contract or the envelope-wrapped form) and skips retrieval; otherwise it runs `retrieve` internally (defaults: mode=thorough, granularity=mixed).
- `synthesize` result = the synthesis output contract; also include `"retrieval"` (the evidence used) so callers can render sources.
- Defaults for `retrieve`: mode=fast, granularity=document (matches "quick lookup" as the common case).
- `sync` refuses (envelope error `invalid_arguments`) if `--root` doesn't exist; `retrieve`/`synthesize` return `index_empty` error if the collection has 0 entries, with message pointing to `vault-rag sync`.

## Step 7 — Streamlit rewiring

- `streamlit_models.py`: build `IndexStore` + `Searcher` from `vault_rag`; cache as today (`st.cache_resource` — check current implementation and preserve it).
- Search page: drop judge UI; add mode (fast/thorough) and granularity selectors; score badges show bm25/semantic/fused/reranker/final from the evidence `scores` dict.
- Synthesize page: call `vault_rag.synthesis.answer.synthesize`; keep the citation-link transformation, now keyed off the `citations[].note_id`.
- Notes page (`streamlit_db.py`): uses `index/reader.py`; only import path changes. When showing entries, filter to `granularity == "document"` so notes aren't listed once per section.
- Run `uv run streamlit run scripts/streamlit_app.py` and click through all three pages before calling this step done.

## Step 8 — Cleanup + docs

1. Delete the old `scripts/*.py` listed in "Deleted" above; grep for stragglers importing them.
2. Update `AGENTS.md`: new commands (`uv run vault-rag sync|retrieve|synthesize`, streamlit unchanged), new architecture section (corpus/index/retrieval/synthesis/cli), env vars (remove `OPENROUTER_JUDGE_MODEL`), note that one note = 1 document entry + N section entries.
3. The collection needs a one-time rebuild (`vault-rag sync --root "./input/Vault 14" --reset`) because entry ids/metadata changed shape. Say so in the final report.

## Tests (pytest; no network — fake provider)

`tests/conftest.py`: `FakeProvider` with deterministic `embed_texts` (e.g., hash-seeded unit vectors), `rerank` returning reversed input order with fake scores, `chat` returning a canned JSON string; a `tiny_vault` fixture writing ~6 markdown files to `tmp_path` (with/without frontmatter, with headings, one > 6000 chars, one tagged `#secret`, one with `updated` frontmatter).

Required cases:
- **frontmatter**: parse/no-frontmatter/bad-yaml passthrough (port behavior from current code).
- **chunker**: heading split levels 1–3; h4 stays inside parent; preamble; fenced code block containing `# not a heading`; oversize split with overlap and correct line ranges; no-headings note = 1 section; line coverage property (every body line in ≥1 section).
- **identity**: frontmatter id wins; fallback = `hash_string(path)`; `is_ulid`.
- **fusion**: RRF and zsigmoid outputs identical to the current implementation for a fixed input (compute expected values with the OLD code before deleting it, hard-code them into the test).
- **store/sync** (patch Chroma with an in-memory fake or use a real `chromadb.PersistentClient` on `tmp_path` — real is fine, it's local): initial sync adds doc+section entries; editing a file re-embeds only that note; deleting a file removes its entries; second sync is a no-op; `#secret` note absent.
- **evidence**: schema keys present; mixed-mode 3-per-note cap; `why` rules; fast mode → `reranker: null`.
- **synthesis**: citation resolution incl. unknown-key warning; abstained propagation; truncated-JSON repair (reuse a truncated fixture string).
- **cli**: run via `subprocess` (`uv run vault-rag ...`) against the tiny vault with the fake provider injected via env… subprocess can't inject the fake — instead call `cli.main()` in-process with `monkeypatch.setattr(sys, "argv", [...])` and the provider factory monkeypatched. Assert envelope shape (`ok`, `action`, `result`), error envelope for empty index, and `schema` output stability.

## Definition of done

- `uv run pytest` green.
- `uv run vault-rag sync --root "./input/Vault 14" --reset` completes; entry count ≈ notes + sections.
- `uv run vault-rag retrieve --query "OpenClaw VPS migration" --mode fast` and `--mode thorough --granularity mixed` return schema-valid JSON (`| jq` parses; spot-check top hits are sensible vs. the golden files from Step 0).
- `uv run vault-rag synthesize --query "What did I write about OpenClaw sandboxing?"` returns an answer with citations resolving to real paths.
- Streamlit three pages work.
- `grep -ri judge --include="*.py" .` → no hits.
- Committed on `phase2-refactor` branch (do not merge without user review).

## Out of scope

- `lint`, `enrich`, `--save` (Phases 3–4).
- Query expansion.
- Any Obsidian-side or bear-side work.
- Re-tuning ranking parameters (keep current defaults; only the judge is removed).
