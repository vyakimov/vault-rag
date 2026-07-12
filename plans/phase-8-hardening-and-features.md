# Phase 8 — Hardening, tightening, and retrieval features

**Executor:** a coding agent working in the `vault-rag` repo.
**Prerequisite (hard):** the bug-fix branch `claude/codebase-review-sey5fn` (commit `2cbfaaa`,
"Fix determinism, sync staleness, privacy, and robustness bugs") must be **merged into main
first**. Every spec below assumes that branch's behavior: stable sorts in fusion, the `warnings`
field in the sync result, frontmatter `#secret`/`#ignore` handling, `tests/test_openrouter.py`,
and pyright/pandas-stubs living in the dev dependency group. If `git log --oneline` on main does
not show that commit (or a merge of it), stop and merge it before doing anything else.

Read `plans/phase-2-vault-rag-refactor.md` and `AGENTS.md` for the package layout and the JSON
envelope/contract conventions referenced throughout.

## Ground rules (apply to every item)

1. **One item = one commit.** Use the item's ID (e.g. `A2`, `C3`) in the commit subject.
   Work through milestones in order: A → B → C → D. Items inside a milestone are ordered by
   dependency; do not reorder across the explicit "depends on" notes.
2. **Run `uv run pytest -q` after every item.** All tests must pass before the commit.
   The suite is network-free (uses `tests/conftest.py`'s `FakeProvider`); keep it that way —
   no test may hit the network or require `OPENROUTER_API_KEY`.
3. **JSON contracts are additive-only.** You may add fields to envelope results and to
   `vault-rag schema` output; you may never rename or remove existing fields, change their
   types, or change error-type strings. `SCHEMA_VERSION` stays `1`.
4. **Style:** absolute intra-package imports (`from vault_rag.corpus import loader`), no import
   fallbacks, match the surrounding code's formatting. Do not run any auto-formatter over
   files you did not otherwise touch.
5. **Do not touch** `plans/` (except the results file at the end), `skills/`, `input/`,
   `chroma_db/`, or `tests/fixtures/notes/`.
6. When an item says "update `AGENTS.md`", edit only the specific line(s) named — `AGENTS.md`
   is the contract other agents read; keep it accurate but minimal.
7. When done with everything, write `plans/phase-8-results.md` summarizing: which items
   shipped, test count before/after, and any deviations from this spec (with reasons).

---

## Milestone A — Tooling & CI

Do this milestone first: it catches regressions introduced by the rest of the phase.

### A1 — Add ruff (lint only, no reformatting)

**Why:** no linter is configured; unused imports and similar rot accumulate silently.

**Files:** `pyproject.toml`, plus whatever files `ruff check` flags.

**Steps:**
1. Add `"ruff"` to the `[dependency-groups] dev` list in `pyproject.toml` and run `uv lock`
   then `uv sync --all-groups`.
2. Add to `pyproject.toml`:
   ```toml
   [tool.ruff]
   line-length = 100
   target-version = "py312"

   [tool.ruff.lint]
   select = ["E4", "E7", "E9", "F", "I"]

   [tool.ruff.lint.isort]
   known-first-party = ["vault_rag"]
   ```
3. Run `uv run ruff check --fix .`, then fix any remaining findings by hand.
   Expected findings are small (import ordering, possibly an unused import). If ruff flags
   something that would change behavior, leave the code alone and add a targeted
   `# noqa: <rule>` with a short reason instead.
4. **Do NOT run `ruff format`** and do not add it to CI. The codebase is hand-formatted;
   formatting churn is explicitly out of scope.

**Done when:** `uv run ruff check .` exits 0 and `uv run pytest -q` passes.

### A2 — GitHub Actions CI

**Why:** the suite is network-free, so CI is nearly free; the flaky-sort bug fixed in the
prerequisite branch would have been caught the day the numpy pin changed.

**Files:** `.github/workflows/ci.yml` (new).

**Spec:** two jobs.
- `test` (blocking): checkout → install uv → `uv sync --all-groups` → `uv run ruff check .`
  → `uv run pytest -q`.
- `types` (non-blocking): same setup → `uv run pyright vault_rag`.
  **Known state: pyright currently reports ~40 pre-existing errors**, almost all
  pandas-stubs friction in `retrieval/` — that is why this job must have
  `continue-on-error: true`. Do NOT attempt to fix them in this phase.

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --all-groups
      - run: uv run ruff check .
      - run: uv run pytest -q

  types:
    runs-on: ubuntu-latest
    continue-on-error: true
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --all-groups
      - run: uv run pyright vault_rag
```

**Done when:** the workflow file is committed and the `test` job passes on the PR/branch run.
(You cannot see Actions results locally; validating the YAML shape and a green local
`ruff check` + `pytest` is sufficient to commit.)

---

## Milestone B — Code tightening

### B1 — `SearchParams` dataclass (refactor `hybrid_search` config plumbing)

**Why:** `Searcher.hybrid_search` (`vault_rag/retrieval/searcher.py`) spends ~40 lines
resolving "explicit argument or `SEARCH_CONFIG` default" for 9 tunables. It's noisy and
untestable.

**Files:** `vault_rag/config.py`, `vault_rag/retrieval/searcher.py`,
`scripts/streamlit_search.py`, new `tests/test_search_params.py`.

**Spec:**
1. In `config.py`, add:
   ```python
   from dataclasses import dataclass, fields, replace

   @dataclass(frozen=True)
   class SearchParams:
       semantic_weight: float = 0.5
       top_k: int = 150                 # candidate pool size
       n_results: int = 10
       combine_strategy: str = "rrf"    # "rrf" | "zsigmoid" | "minmax"
       rrf_k: int = 60
       zsigmoid_temperature: float = 1.0
       rerank_top_k: int = 30
       rerank_use_ranks: bool = True
       recency_boost_enabled: bool = True
       recency_weight: float = 0.2
       recency_decay_days: float = 365.0

       def with_overrides(self, **overrides) -> "SearchParams":
           provided = {k: v for k, v in overrides.items() if v is not None}
           return replace(self, **provided)

   DEFAULT_SEARCH_PARAMS = SearchParams()
   ```
   Keep the existing `SEARCH_CONFIG` dict **unchanged and in place** for now (it documents
   the same defaults); add a comment on it: `# Deprecated: read DEFAULT_SEARCH_PARAMS.
   Kept only until external readers migrate.` Keep `BM25_CONFIG` as-is.
2. In `hybrid_search`, keep the **public signature exactly as it is** (all existing keyword
   arguments, all defaulting to `None`) so no caller or test changes are forced. Replace the
   per-variable resolution block with:
   ```python
   params = DEFAULT_SEARCH_PARAMS.with_overrides(
       semantic_weight=semantic_weight,
       top_k=top_k,
       n_results=n_results,
       combine_strategy=combine_strategy,
       rrf_k=rrf_k,
       zsigmoid_temperature=zsigmoid_temperature,
       recency_boost_enabled=recency_boost_enabled,
       recency_weight=recency_weight,
       recency_decay_days=recency_decay_days,
   )
   ```
   then use `params.<field>` everywhere below (note: `strategy = params.combine_strategy.lower()`).
   The rerank pool size becomes `params.rerank_top_k`; the rank-vs-score toggle becomes
   `params.rerank_use_ranks` (drop the two remaining `SEARCH_CONFIG.get` calls inside the
   rerank block).
3. `scripts/streamlit_search.py` reads `SEARCH_CONFIG.get("n_results", 10)` for the
   number-input default — switch it to `DEFAULT_SEARCH_PARAMS.n_results`.
4. The `debug_info` dict keys and values must remain byte-identical to today (contract-ish:
   the CLI exposes it as `meta.tunables`).

**Tests (`tests/test_search_params.py`):**
- `with_overrides` ignores `None`s and applies non-`None`s.
- `DEFAULT_SEARCH_PARAMS` matches every value in `SEARCH_CONFIG` (guards drift while the
  dict still exists) — iterate `fields(SearchParams)` and compare against the dict entries
  that exist (`top_k` maps to `default_top_k`).
- Existing retrieval tests keep passing untouched.

### B2 — Short-circuit the phrase scan in `calculate_keyword_scores`

**Why:** `Searcher.calculate_keyword_scores` normalizes **every document on every query**
(`normalize_no_punct(doc)`) even when the query has no quoted phrases — which is the common
case, and this is the per-query hot path.

**Files:** `vault_rag/retrieval/searcher.py`, `tests/` (extend an existing searcher-adjacent
test file or add `tests/test_searcher_unit.py`).

**Spec:** after computing `bm25_scores`, extract phrases first; if there are none, return
immediately without touching the documents:
```python
query_tokens = tokenize_for_bm25(query, self.stop_words, self.stemmer)
bm25_scores = bm25.get_scores(query_tokens)
_, _, quoted_phrases = self.extract_important_terms(query)
if not quoted_phrases:
    return pd.Series(
        dict(zip(ids, (float(s) for s in bm25_scores))),
        dtype=float, name="keyword_scores",
    )
# ... existing per-document phrase-boost loop unchanged ...
```

**Tests:** build a real `IndexStore` from `tiny_vault` (pattern: `tests/test_store_sync.py`)
and a `Searcher` with `FakeProvider`; assert (a) an unquoted query returns the same scores
as today (compare against `bm25.get_scores` directly), (b) a query with `"zqxq"` quoted
still applies the `* 1.3` boost to documents containing the phrase.

### B3 — Lazy per-granularity BM25 build in `IndexStore`

**Why:** `_rehydrate_from_collection` tokenizes and builds BM25 for **both** granularities on
every `IndexStore` construction — i.e. every CLI invocation — even though e.g.
`retrieve --granularity document` only ever touches the document index. Tokenization is the
expensive part (Porter-stems the whole corpus).

**Files:** `vault_rag/index/store.py`, `tests/test_store_sync.py`.

**Spec:**
1. `_rehydrate_from_collection` keeps loading `documents`/`ids`/`metadatas` eagerly (cheap,
   and `get_collection_stats` needs metadatas), but no longer tokenizes or constructs
   `BM25Okapi`. It resets `self.tokenized[g] = []` and `self.bm25[g] = None` for both
   granularities.
2. Add a private `_ensure_bm25(self, granularity: str) -> None` that builds
   `self.tokenized[granularity]` and `self.bm25[granularity]` if `self.bm25[granularity] is
   None` and `self.documents[granularity]` is non-empty. Idempotent.
3. `granularity_data()` calls `self._ensure_bm25(granularity)` before returning. That is the
   only call site the searcher needs; nothing in `Searcher` changes.
4. `sync()` already ends with `_rehydrate_from_collection()`; after this change a sync
   simply leaves BM25 unbuilt until first search — that is correct and intended.
5. Update `tests/test_store_sync.py::test_rehydrate_on_new_store_instance`: it currently
   asserts `fresh.bm25["document"] is not None` right after construction. Change it to
   assert both are `None` after construction, then call
   `fresh.granularity_data("document")` and assert `fresh.bm25["document"] is not None`
   while `fresh.bm25["section"] is None` (proves laziness is per-granularity), then
   `granularity_data("section")` and assert both built.

**Done when:** the updated test passes and no other test changed.

### B4 — `lint.py` performance and cleanup

**Why:** the stale-distilled check resolves each source link with
`next((n for n in notes if n.path == resolved), None)` — O(notes²) on link-heavy vaults.
And `_sources_wikilinks` has a redundant branch.

**Files:** `vault_rag/compounding/lint.py`.

**Spec:**
1. In `lint_vault`, right after the `notes` list is built, add
   `notes_by_path = {note.path: note for note in notes}` and replace the `next(...)` lookup
   in the stale-distilled loop with `notes_by_path.get(resolved)`.
2. In `_sources_wikilinks`, the fence branch reads:
   ```python
   if line.strip().startswith("```"):
       in_fence = not in_fence
       if collecting:
           continue
       continue
   ```
   Both paths `continue`; collapse to toggle + single `continue`.
3. No behavior change: the existing `tests/test_lint.py` must pass untouched.

### B5 — Dead code removal in `utils.py`

**Why:** `count_tokens` has no callers; `decimal_to_base` carries an unreachable
`base > 62` fallback and a pointless `conversion_table` parameter.

**Files:** `vault_rag/utils.py`, `tests/test_utils.py`.

**Spec:**
1. Delete `count_tokens`.
2. Simplify `decimal_to_base(n)` to the base-62 case only (drop the `base` and
   `conversion_table` parameters and the `chr(x + 55)` branch).
   **CRITICAL INVARIANT:** `hash_string` output must be byte-identical before and after —
   note ids for notes without frontmatter `id` are `hash_string(relative_path)` and a
   changed encoding would orphan/duplicate every such note in existing indexes. Before
   committing, add a golden test locking it down, e.g.:
   ```python
   def test_hash_string_golden():
       # Frozen: note identity depends on this exact output.
       assert hash_string("folder/note.md") == "<capture the CURRENT output and paste it here>"
   ```
   Capture the golden value by running the function on **unmodified** code first.
3. Keep `is_ulid` in `corpus/identity.py` (it is exercised by tests and documents the id
   format). Remove any `tests/test_utils.py` tests that covered only the deleted code.

### B6 — Streamlit Synthesize page: safe init + explicit trigger

**Why:** `scripts/streamlit_llm.py` builds the OpenRouter client at module import
(`client = get_openrouter_client()` at top level) — a missing `.env` crashes the page with a
raw traceback, unlike the other pages. It also fires the LLM call automatically the moment
the page renders, spending tokens on a mere tab click.

**Files:** `scripts/streamlit_llm.py`.

**Spec:**
1. Remove the module-level `client = ...`. Inside the page body (the `else:` branch where
   `last_results` exists), obtain the client lazily:
   ```python
   try:
       client = get_openrouter_client()
   except Exception as exc:
       st.error(f"OpenRouter is not configured: {exc}")
       st.stop()
   ```
2. Replace the auto-run block:
   ```python
   if st.session_state["llm_response"] is None:
       with st.spinner("Synthesizing..."):
           ...
   ```
   with an explicit button. Render a primary button `"🤖 Synthesize"` when
   `llm_response is None`; only on click run the spinner + `synthesize(...)` and
   `st.rerun()`. Keep the existing `"🔄 Regenerate"` button (which clears `llm_response`)
   — after regeneration the user clicks Synthesize again.
3. When `llm_response` is not `None`, render `write_response(...)` exactly as today.

**Done when:** manual check — `uv run streamlit run scripts/streamlit_app.py` with no
`.env`: the Synthesize tab shows the friendly error, not a traceback. (No automated test;
Streamlit pages are untested by design in this repo.)

### B7 — Accept `--chroma-path`/`--collection` after the subcommand

**Why:** `vault-rag sync --chroma-path x --root y` fails today because the two flags are
defined only on the top-level parser; users reasonably type them after the subcommand.

**Files:** `vault_rag/cli.py`, `tests/test_cli.py`.

**Spec:** in `build_parser`, create a parent parser and attach it to every subcommand:
```python
common = argparse.ArgumentParser(add_help=False)
common.add_argument("--chroma-path", default=argparse.SUPPRESS, help="Chroma persistence dir")
common.add_argument("--collection", default=argparse.SUPPRESS, help="Chroma collection name")
```
- Keep the existing two `parser.add_argument` lines on the main parser **unchanged** (they
  carry the real defaults `"chroma_db"` / `"vault_notes"`).
- Add `parents=[common]` to **every** `sub.add_parser(...)` call.
- The `default=argparse.SUPPRESS` on the parent is the load-bearing detail: without it, the
  subparser's default would overwrite a value given before the subcommand. With SUPPRESS,
  the subparser only sets the attribute when the flag actually appears after the
  subcommand.

**Tests:** three cases through `cli.main` with a monkeypatched `get_provider` (pattern:
`TestEnvelopeShape`): flag before subcommand (existing test already covers), flag after
subcommand, and flag absent (defaults). Assert via a `sync` against `tiny_vault` writing
into `tmp_path` chroma dirs that the chosen path is honored (e.g. the directory exists
afterwards).

### B8 — Document the real `mixed` granularity semantics

**Why:** `mixed` does **not** search both pools; it searches the section pool and caps
results at 3 sections per note (`Searcher.hybrid_search`: `data_granularity = "section" if
granularity == "mixed"`). The schema string `document|section|mixed` implies otherwise.
Decision (do not re-litigate): keep the behavior, fix the documentation.

**Files:** `vault_rag/cli.py` (`_schema()`), `AGENTS.md`.

**Spec:**
1. In `_schema()`, change both `--granularity` arg descriptions to:
   `"document|section|mixed (mixed = section pool, max 3 sections per note; documents are not searched)"`.
2. In `AGENTS.md`, on the `retrieve` key-commands line, append after the defaults sentence:
   `"`mixed` searches the section pool with a 3-sections-per-note cap (it does not mix in document entries)."`
3. `tests/test_cli.py::test_schema_is_stable` asserts structure, not strings — it must
   still pass unmodified.

---

## Milestone C — CLI features

### C1 — `vault-rag stats`

**Why:** `IndexStore.get_collection_stats()` exists but is unreachable from the CLI;
obsctl and the vault skill want a cheap index-health probe that works **without an API key**.

**Files:** `vault_rag/index/reader.py`, `vault_rag/cli.py`, `tests/test_cli.py`.

**Spec:**
1. Use `DatabaseReader` (no provider, no API key), not `IndexStore`. Extend
   `DatabaseReader.get_collection_stats()` to also return:
   - `"section_entries"`: count of metadatas with `granularity == "section"`,
   - `"embedding_model"`: `self.collection.metadata.get("embedding_model", "unknown")`
     (guard `self.collection.metadata` possibly `None`).
   Keep all existing keys.
2. New handler `cmd_stats`:
   - Construct `DatabaseReader(args.chroma_path, args.collection)`.
   - If `reader.collection is None` **or** `reader.collection.count() == 0` → `failure("stats",
     "index_empty", "index is empty; run `vault-rag sync --root <dir>` first")`.
   - Else `success("stats", result=reader.get_collection_stats())`.
3. Register in `build_parser` (`sub.add_parser("stats", parents=[common], help="Index statistics")`)
   and `_HANDLERS`. Add to `_schema()["commands"]`:
   ```python
   "stats": {"args": {}, "result": {
       "total_documents": "int", "total_entries": "int", "section_entries": "int",
       "unique_folders": "int", "unique_tags": "int", "dated_notes": "int",
       "embedding_model": "str"}},
   ```
4. Add one line to `AGENTS.md` Key Commands: `- uv run vault-rag stats — index statistics
   (no API key needed).`

**Tests:** (a) stats on an empty/missing chroma dir → `index_empty`; (b) sync `tiny_vault`
then stats → `ok: true`, `total_documents == 5`, `section_entries >= 5`,
`embedding_model == "fake-embed"`. Note `cmd_stats` must NOT call `get_provider()` — test
(b) can pass `monkeypatch.setattr(cli, "get_provider", ...)` only for the sync call, then
`monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)` before stats to prove it.

### C2 — `sync --dry-run`

**Why:** before a big backfill or vault reorganization, users want to see what sync *would*
do without paying for embeddings or mutating the index.

**Files:** `vault_rag/index/store.py`, `vault_rag/cli.py`, `tests/test_store_sync.py`,
`tests/test_cli.py`.

**Spec:**
1. `IndexStore.sync(root, reset=False, dry_run=False)`. Behavior with `dry_run=True`:
   - `reset=True` together with `dry_run=True` is a caller error; `cmd_sync` rejects the
     combination with `invalid_arguments` **before** touching the store.
   - Run the load + dedupe + diff exactly as today, but: no `collection.delete`, no
     `provider.embed_texts`, no `collection.add`, no `_rehydrate_from_collection`.
   - Result: the same count fields as today (counts of what WOULD happen), plus
     `"dry_run": true` and three lists:
     `"would_add": [paths]`, `"would_update": [paths]`, `"would_delete": [stored paths]`
     (for deletes use the `path` recorded in the existing group's metadata; fall back to
     the note_id string if empty). Sort each list. `total_entries` = current
     `collection.count()` (unchanged index).
   - The non-dry-run result additionally gains `"dry_run": false` (additive, harmless).
2. CLI: `p_sync.add_argument("--dry-run", dest="dry_run", action="store_true")`; pass it
   through. Schema: add `"--dry-run": "flag"` to sync args and `"dry_run": "bool"` +
   `"would_add/would_update/would_delete": ["str"] (dry-run only)` to the result doc.

**Tests:** sync `tiny_vault` normally; edit one note, delete another, create a third; run
`sync --dry-run`: assert the three lists name exactly the right paths, counts match, the
provider recorded **zero** embed calls during the dry run, and `collection.count()` is
unchanged; then run a real sync and assert it performs exactly what the dry run predicted.

### C3 — Metadata filters for `retrieve`/`synthesize` (+ expose `--must-include`)

**Why:** retrieval currently searches the whole vault. Folder/tag/type/date scoping is the
most-requested kind of narrowing, and `hybrid_search` already has the mechanism (the
`allowed_ids` set used by `must_include_terms`) — it's just not exposed.

**Files:** `vault_rag/retrieval/searcher.py`, `vault_rag/cli.py`, `vault_rag/retrieval/evidence.py`,
`tests/test_cli.py`, new `tests/test_filters.py`.

**Design decision (do not re-litigate):** all filters are applied **Python-side** by
shrinking `allowed_ids` from metadata, NOT via Chroma `where` clauses (tags are stored as a
comma-joined string, so Chroma equality can't express tag membership; one mechanism for all
filters beats two). Consequence to accept: the semantic candidate pool is fetched pre-filter,
so a very narrow filter may leave only BM25-sourced candidates. That is fine.

**Spec:**
1. `hybrid_search` gains keyword-only params (all default `None`):
   `folder: Optional[str]`, `tags: Optional[List[str]]`, `note_type: Optional[str]`,
   `since: Optional[str]`, `until: Optional[str]` (ISO date or datetime strings).
2. Right after the existing `must_include_terms` block, filter `allowed_ids` further. Match
   rules per entry metadata `m`:
   - **folder**: `m["folder"] == folder or str(m["folder"]).startswith(folder.rstrip("/") + "/")`
     (so `--folder Projects` matches `Projects` and `Projects/Sub`).
   - **tags**: note tags = `{t.strip().lower() for t in str(m.get("tags", "")).split(",") if t.strip()}`;
     every requested tag (lowercased) must be in that set (AND semantics).
   - **note_type**: case-insensitive equality with `m.get("note_type", "")`.
   - **since/until**: resolve the entry date as `m.get("updated") or m.get("date") or ""`;
     parse with `datetime.fromisoformat(raw.replace("Z", "+00:00"))`, treating naive as UTC
     (same recipe as `calculate_recency_scores`). A note with no parseable date is
     **excluded** whenever `since` or `until` is given. Parse the `since`/`until` strings
     the same way once, up front; a date-only string like `2025-01-01` is valid
     (`fromisoformat` accepts it; attach UTC). Invalid `--since/--until` input →
     `ValueError` with a clear message (the CLI maps it to `not_found` today — instead,
     validate in `cmd_retrieve`/`cmd_synthesize` first and return `invalid_arguments`).
   - Empty `allowed_ids` after filtering → the existing
     `ValueError("No documents match the required terms.")` path — reword that message to
     `"No documents match the required filters."`
3. Record applied filters in `debug_info["filters"] = {"folder": ..., "tags": ...,
   "note_type": ..., "since": ..., "until": ..., "must_include": ...}` (only non-None keys).
4. CLI: on **both** `retrieve` and `synthesize` subparsers add
   `--folder`, `--tag` (append action, repeatable), `--type` (dest `note_type`),
   `--since`, `--until`, `--must-include` (append action → `must_include_terms`).
   Thread them through `_run_retrieval` into `hybrid_search`. Update `_schema()` args for
   both commands.
5. `build_retrieval_output` stays unchanged (filters already surface via `meta.tunables`).

**Tests (`tests/test_filters.py`, using `tiny_vault` + `FakeProvider`):**
- `--type`: only `note_updated.md` has... (tiny_vault has no `type` frontmatter — extend the
  fixture in `conftest.py` by adding `type: recipe` to `note_a.md`'s frontmatter; then a
  `note_type="recipe"` search returns only Alpha-note entries, and existing tests'
  counts are unaffected since no test asserts on `note_a`'s frontmatter keys).
- `tags=["gamma"]` → only `note_updated.md`.
- `folder` filter: fixture notes are all at vault root (`folder == "."`); create one note in
  a subfolder inside the test itself, sync, filter on the subfolder.
- `since="2025-01-01"` → only `note_updated.md` (updated 2025-06-15); notes without dates
  excluded; `until="2024-12-31"` excludes it.
- Impossible combination (e.g. `tags=["nope"]`) → CLI envelope `not_found`.
- CLI passthrough: `retrieve --tag gamma` end-to-end returns only gamma candidates.

### C4 — Lint: `duplicate_titles` check + `--fix` for missing contract fields

**Why (titles):** distilled notes link sources by **title** (`distill._link_targets`
handles collisions only among one answer's citations); vault-wide title collisions make
those wikilinks ambiguous in Obsidian, and nothing reports them today.
**Why (fix):** lint already finds missing `id`/`created`/`updated`, and `tools/backfill.py`
already knows how to fix exactly that — but only as a standalone one-shot script.

**Files:** `vault_rag/compounding/lint.py`, new `vault_rag/compounding/backfill_core.py`,
`tools/backfill.py`, `vault_rag/cli.py`, `tests/test_lint.py`, `tests/test_backfill.py`.

**Spec, part 1 — `duplicate_titles`:**
1. New findings key `duplicate_titles` (and summary count). Two or more notes whose
   effective title (frontmatter `title` or filename stem, exactly as `NoteInfo.title` is
   built today) compares equal **case-insensitively** → one finding
   `{"title": <as written on the first note>, "paths": sorted([...])}`.
2. Distilled notes participate like any other note. Order findings by title.

**Spec, part 2 — extract backfill core:**
1. Create `vault_rag/compounding/backfill_core.py` and MOVE (not copy) these from
   `tools/backfill.py`: `TIMESTAMP_POLICY`, `ULID_RE`, `LEGACY_ID_FIELDS`,
   `format_timestamp`, `iso_to_policy`, `now_timestamp`, `GitContext`, `git_context`,
   `_git_dates`, `git_first_commit`, `git_last_commit`, `Change`, `_legacy_id_values`,
   `_closing_fence`, `detect_ambiguity`, `resolve_id`, `resolve_created`, `resolve_updated`,
   `apply_changes_to_text`, `_as_str`.
2. `tools/backfill.py` imports all of those from `vault_rag.compounding.backfill_core`
   (keep `SKIP_DIRS`, scanning, report building, console output, and `main` in the tool).
   Its CLI behavior and `tests/test_backfill.py` must not change (the tests import
   `backfill` and call `build_report` — that still lives in the tool).
3. Keep the clamp logic where it is (in `build_report`); it is report-level, not core.

**Spec, part 3 — `lint --fix`:**
1. CLI: `p_lint.add_argument("--fix", action="store_true", help="Write missing id/created/updated frontmatter")`.
2. Semantics: `--fix` ONLY addresses `missing_frontmatter_fields`. For each note in that
   findings list (re-derived, see below), apply the same resolution as backfill:
   - Skip any note where `detect_ambiguity(...)` fires (same inputs as backfill: its
     frontmatter, raw text, and an id-count map built over the scanned notes); record it
     under a new result key instead of writing.
   - Compute changes via `resolve_id` / `resolve_created` / `resolve_updated` with
     `git_context(root)` and the file's `stat()`; apply the same updated-vs-created clamp
     as `tools/backfill.py`; write with `apply_changes_to_text`. Bodies are never touched.
3. Envelope result with `--fix`: run lint first, apply fixes, then **re-run lint** and
   return the post-fix report plus two extra keys:
   `"fixed": [{"path": ..., "fields": ["id", ...]}]` and
   `"fix_skipped": [{"path": ..., "reason": ...}]`. `--fix` combined with `--format text`
   prints the text report of the post-fix state (plus a `fixed: N` line).
4. Schema: add `--fix` to lint args; note the two additive result keys.
5. Update the `AGENTS.md` lint line: `lint` is read-only **unless `--fix`**, which writes
   only missing `id`/`created`/`updated` frontmatter.

**Tests:**
- `duplicate_titles`: two fixture notes titled `Same` / `same` → one finding; unique titles
  → none. Existing lint tests unchanged.
- `--fix` via `cli.main` on a tmp vault: note without frontmatter gains all three fields
  (parse back with `split_frontmatter`, assert ULID shape and offset-aware timestamps);
  post-fix report shows `missing_frontmatter_fields == 0`; body byte-identical; a note with
  unparseable frontmatter lands in `fix_skipped` and is not modified (compare bytes).
- `tests/test_backfill.py` passes unmodified (proves the extraction preserved behavior).

---

## Milestone D — Performance & quality features

### D1 — Section-level embedding reuse on sync

**Why:** editing one paragraph of a large note currently re-embeds ALL of its sections plus
the document entry. Reusing embeddings for unchanged sections makes the common
small-edit case nearly free. This matters as Phase-6 capture increases write frequency.

**Files:** `vault_rag/index/store.py`, `tests/test_store_sync.py`.

**Design:** no separate cache file — Chroma already stores every embedding. Reuse by
matching a per-entry content hash.

**Spec:**
1. In `_entries_for_note`, add to every entry's metadata (document and section):
   `"entry_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()` where `text` is exactly
   the string that gets embedded (the same value placed in the entry tuple). Import
   `hashlib` in `store.py`.
2. In `sync()`, for **updated** notes only (the `content_hash`/`path` mismatch branch),
   before extending `ids_to_delete`, fetch the old entries' embeddings:
   ```python
   old = self.collection.get(ids=group["ids"], include=["embeddings", "metadatas"])
   ```
   Build `reusable: Dict[str, List[float]]` mapping `metadata["entry_hash"]` →
   embedding for every old entry that has a non-empty `entry_hash`. (Chroma may return
   numpy arrays; coerce with `[float(x) for x in emb]`.) Collect these per-note maps into
   one dict for the whole sync run (hash collisions across notes are fine — same text,
   same embedding).
3. Where embeddings are computed today:
   ```python
   embeddings = self.provider.embed_texts(add_texts, batch_size=32)
   ```
   replace with a partition: for each entry, look up its metadata `entry_hash` in
   `reusable`; embed only the misses (in original order), then reassemble the full
   embeddings list in entry order. Keep the `embed_texts` call signature unchanged.
4. Old collections synced before this change have no `entry_hash` in metadata → `reusable`
   stays empty → behavior identical to today. No migration, no reset required (entries
   gain `entry_hash` as their notes change). State this in the commit message.
5. Do not attempt reuse for *unchanged* notes (they aren't re-embedded at all) or for
   *added* notes (nothing to reuse).

**Tests:**
- Multi-section note (`note_a.md` has 3+ entries): sync, clear `embed_calls`, append a line
  to ONE section's text, sync again. Assert `updated_notes == 1` and the texts passed to
  `embed_texts` (via `fake_provider.embed_calls`) contain **only** the document entry and
  the changed section — not the untouched sections.
- Moved note (path-only change): every section text embeds `# {title}` + section text —
  title and section text are unchanged by a move, but the document entry embeds the path,
  so assert exactly one text (the document entry) is re-embedded.
- Sanity: after the partial re-embed, `collection.count()` equals doc+section totals and a
  `granularity_data` search still works.

### D2 — On-disk query-embedding cache

**Why:** repeated queries (Streamlit reruns, retrieve-then-synthesize, iterating on
`--n-context`) pay for the same query embedding every time.

**Files:** new `vault_rag/retrieval/query_cache.py`, `vault_rag/retrieval/searcher.py`,
new `tests/test_query_cache.py`.

**Spec:**
1. `query_cache.py`:
   ```python
   class QueryEmbeddingCache:
       def __init__(self, path: str, model: str, max_entries: int = 256): ...
       def get(self, query: str) -> Optional[List[float]]: ...
       def put(self, query: str, embedding: List[float]) -> None: ...
   ```
   - Storage: single JSON file `{"model": str, "entries": {key: {"embedding": [...], "ts": float}}}`.
   - `key = hashlib.sha256(query.encode("utf-8")).hexdigest()`.
   - Load lazily on first `get`/`put`. A missing, unreadable, or JSON-invalid file, or one
     whose `"model"` differs from `self.model`, is treated as empty (and overwritten on the
     next `put`). Never raise out of `get`/`put` — cache failures must not break retrieval;
     swallow `OSError`/`ValueError` and behave as a miss/no-op.
   - `put` sets `ts = time.time()`, evicts oldest-`ts` entries beyond `max_entries`, and
     writes atomically: write to `path + ".tmp"` then `os.replace`.
2. Wire-up in `Searcher.hybrid_search`: replace
   `query_embedding = self.provider.embed_texts([query])[0]` with a small helper
   `self._embed_query(query)` that:
   - lazily constructs `QueryEmbeddingCache(os.path.join(self.store.chroma_db_path,
     "query_embedding_cache.json"), self.store.provider.embedding_model)` once per Searcher
     (guard with `getattr(self.store, "chroma_db_path", None)` — if absent, skip caching),
   - returns the cached embedding on hit; on miss embeds and `put`s.
3. Add `"query_cache": "hit"|"miss"|"off"` to `debug_info`.

**Tests (`tests/test_query_cache.py`):**
- Unit: put/get roundtrip; model mismatch invalidates; corrupt file tolerated; eviction cap
  honored (insert `max_entries + 5`, assert size and that the newest survive).
- Integration: sync `tiny_vault`, run the same `hybrid_search` twice with `FakeProvider`;
  assert the second run performed no `embed_texts` call for the query (count
  `embed_calls`), results identical, and `debug_info["query_cache"]` reads `"miss"` then
  `"hit"`.

### D3 — Citation-coverage warnings in synthesis

**Why:** the abstention story checks that cited keys exist, but an answer can still contain
whole uncited sentences. Surfacing that (as a warning, not a rejection) strengthens
`--save` gating and user trust.

**Files:** `vault_rag/synthesis/answer.py`, `tests/test_synthesis.py`.

**Spec:** in `synthesize()`, after the citations loop and before building the return dict,
when `parsed` is not None and not `abstained` and the answer is a non-empty string:
```python
sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", str(parsed.get("answer", ""))) if s.strip()]
uncited = [s for s in sentences if len(s) >= 40 and not re.search(r"\[S\d+", s)]
if uncited:
    warnings.append(f"{len(uncited)} sentence(s) lack citations")
```
- The 40-char floor skips headers/transitions ("In short:"). Do not tune it further.
- This is a warning only; it must NOT change `abstained`, `confidence`, or `--save`
  behavior (distill gates on abstained/confidence/citations, not warnings — leave that).

**Tests:** (a) answer where every long sentence carries `[S0]` → no new warning;
(b) two long uncited sentences → exactly `"2 sentence(s) lack citations"`;
(c) abstained answers and empty answers → no coverage warning;
(d) existing synthesis tests untouched and passing (the canned `"Canned."` answer is
under 40 chars, so it stays warning-free).

---

## Recommended execution order

`A1 → A2 → B1 → B2 → B3 → B4 → B5 → B6 → B7 → B8 → C1 → C2 → C3 → C4 → D1 → D2 → D3`

Rationale: A gives you CI; B1 must precede C3 (both touch `hybrid_search` — doing C3 first
would make B1 a painful rebase); C2 before D1 (both restructure `sync()`; C2's dry-run
carve-out is simpler to land first); D-items are independent of each other.

If you must cut scope, cut from the end (D3, D2, D1 are each independently droppable);
do not cut A or B1.

## Definition of done (phase level)

- All items committed individually, `uv run pytest -q` green (expect roughly 175+ tests, up
  from 146), `uv run ruff check .` clean.
- `vault-rag schema` output contains the new `stats` command, sync `--dry-run`, retrieve/
  synthesize filter args, and lint `--fix` — and nothing pre-existing changed shape.
- Manual smoke on the dev corpus (`./input/Vault 14`, requires a real API key — skip
  gracefully if unavailable and say so in the results file):
  `sync --dry-run` → `sync` → `stats` → `retrieve --tag <known-tag>` →
  `retrieve --query "..." ` twice (second run logs a cache hit) → `lint --fix` on a COPY of
  the corpus, never the original.
- `plans/phase-8-results.md` written (see ground rule 7).

## Out of scope (do not do these even if tempting)

- Fixing the ~40 pre-existing pyright errors or making the `types` CI job blocking.
- `ruff format` / any repo-wide reformatting.
- Changing `mixed` granularity to actually merge document+section pools (B8 documents
  current behavior instead — a pool merge changes ranking and needs its own evaluation).
- MMR/diversity, HyDE, multi-query expansion, or any retrieval-quality experiments.
- Replacing nltk with a lighter stemmer (stem outputs differ between implementations and
  would silently change BM25 tokenization; needs its own migration plan).
- Auto-regenerating stale distilled notes; obsctl changes; touching the live vault.
- Caching BM25/tokenized corpora on disk (B3's lazy build is the sanctioned scope).
