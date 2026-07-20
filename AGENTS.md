# Assistant.md

This repository is **Vault Spider**, a retrieval **and mutation** system for an Obsidian vault of
Markdown notes — one CLI for querying and modifying the vault.

The vault itself is never committed. Its location, and everything else installation-specific,
lives in `config.yaml` (gitignored; copy `config.yaml.example`). Secrets live in `.env`.

## Project Overview

The project indexes `.md` files from the vault into ChromaDB, builds BM25 indexes, and exposes
hybrid retrieval and answer synthesis with stable JSON contracts. Those contracts are also exposed
through a dual-transport MCP server for Claude Desktop and ChatGPT. It also carries the vault's
write path: contract-enforcing note mutations executed through the running Obsidian app (the
former standalone `obsctl` tool, merged into this CLI). It is organized as the `vault_spider`
Python package with a JSON-only CLI (`vault-spider`) plus a Streamlit UI.

The read path (indexing, retrieval, lint) works on vault files directly; the write path goes
through the official Obsidian CLI so wikilinks update on move/rename, unknown frontmatter keys
survive patches, and vault plugins fire. Keep that boundary: **never write vault files directly
from mutation code.**

One note is indexed as **one `document`-granularity entry plus N `section` entries** (section
splitting is deterministic, heading-based). Both live in a single Chroma collection, distinguished
by the `granularity` metadata field.

Notes carry a **`provenance`** frontmatter field recording who authored their words:
`human` (typed by the vault owner), `reference` (imported external human-authored content —
papers, webpages, blog posts), `llm` (imported LLM output, e.g. pasted chats), or `distilled`
(generated from the vault by `synthesize --save`; regenerable). Provenance is set at creation
by the capture flow, immutable once set, never proposed by enrichment, and is orthogonal to
`type` (which classifies *kind*: recipe, runbook, ...). Synthesis trusts `human` notes over
imported ones and treats `distilled` notes as pointers; retrieval filters on it
(`--provenance`); `tools/backfill_provenance.py` stamps existing vaults (dry-run by default).

## Key Commands

- `./bin/vault-spider` is the stable executable wrapper for the CLI. It locates this project and
  delegates to `uv run vault-spider`, so its absolute path can be called from outside the repository.
- `./bin/vault-spider schema`
  - Prints the machine-readable command + contract schema (`version: 2`).
- `./bin/vault-spider sync [--root <dir>] [--reset]`  (`--root` defaults to `vault.root` in
  `config.yaml`, then the active Obsidian vault)
  - Incremental sync: adds new notes, re-embeds changed or moved notes, deletes removed notes.
    Notes sharing a duplicate frontmatter `id` are skipped after the first and reported in
    the result's `warnings`.
  - Failure-safe ordering: old entries are deleted only after all new embeddings have been
    computed and validated, so a provider failure mid-sync leaves the existing index usable.
  - `--reset` rebuilds the collection from scratch (needed once after an entry-shape change).
- `./bin/vault-spider stats` — index statistics (no API key needed).
- `./bin/vault-spider retrieve --query "..." [--mode fast|thorough] [--granularity document|section|mixed] [-n 10] [--folder ...] [--tag ...] [--type ...] [--provenance human|reference|llm|distilled] [--since ...] [--until ...]`
  - Returns the retrieval output contract (candidates with score breakdown). Defaults: `fast`, `document`.
    `mixed` searches the section pool with a 3-sections-per-note cap (it does not mix in document entries).
  - `fast` skips reranking; `thorough` reranks the top candidates.
- `./bin/vault-spider synthesize --query "..." [--mode thorough] [--granularity mixed] [--retrieval file.json] [--n-context 8] [--save --root <dir> [--save-dir Distilled]]`
  - Retrieves (defaults `thorough`/`mixed`) then synthesizes a cited answer. `--retrieval` reuses a
    prior `retrieve` envelope/contract and skips retrieval. Abstains when the notes lack the answer.
  - `--save` persists a high-quality answer as a create-only **distilled note** (`type: distilled`)
    under `<root>/<save-dir>`. `--save-dir` must be a vault-relative path resolving inside the
    root. Skips (with a warning) when the answer abstained, is low-confidence, is empty, has no
    citations, or the target exists. Distilled notes are regenerable pointers to their
    sources — raw notes always win on conflict. Run `vault-spider sync` afterward to index it.
- `./bin/vault-spider lint --root <dir> [--format json|text] [--fix] [--fix-timestamps]`
  - Read-only corpus health report (no LLM or index needed): missing frontmatter fields,
    invalid/policy-mismatched timestamps, duplicate ids, duplicate titles, broken wikilinks, `dangling_targets`
    (unresolved link targets ranked by how many notes want them — the best next notes to write),
    `empty_notes` (stubs, ranked by inbound links), `conflict_copies` (`Note 1.md` beside
    `Note.md`), orphans, stale distilled notes, and `imported_missing_source`
    (`reference`/`llm`-provenance notes without a `source_url`).
  - Link resolution follows Obsidian: frontmatter links (`parents: "[[Daily Notes]]"`) count as
    real edges, `aliases` resolve, and `[[diagram.png]]` resolves to an attachment rather than
    being reported broken.
  - `--fix` writes only *missing* `id`/`created`/`updated` frontmatter (never edits a value).
    `--fix-timestamps` normalizes parseable `created`/`updated`/`date` values to
    `timestamps.policy`; unparseable values are skipped, never guessed. `offset_local` and
    `utc_z` are offset-aware. `obsidian_local` writes local `YYYY-MM-DDTHH:mm:ss` values so
    Obsidian Date & time properties use the OS-localized renderer. Normalization preserves file
    mtimes so the formatting-only migration does not trigger false recency.
- `./bin/vault-spider enrich --root <dir> (--note <path> | --stdin) [--intent ...] [--source-url ...] [--title ...]`
  - App-agnostic **enrichment planner**: retrieves a note's neighborhood and proposes a title,
    frontmatter patch (`type`/`aliases`/`source_url` only), inline links, related candidates,
    and placement — as JSON. It **never mutates** files or the index; apply a plan with the
    mutation commands below. Only links to retrieved neighbors are proposed; the LLM output is
    validated in code (confidence gating, anchor resolution, existing-type/link guards, unsafe
    titles and non-numeric confidences dropped with warnings). Enrich never proposes
    `provenance` — that field is set by whichever flow creates the note, not inferred from
    content. `--note` must be a vault-relative `.md` path resolving inside `--root`.
- `./bin/vault-spider eval validate --dataset eval` / `./bin/vault-spider eval run --dataset eval [--stage retrieval|synthesis] [--mode thorough] [--granularity mixed] [-n 10] [--k 5] [--only <qid>] [--out results.json]`
  - Golden-dataset benchmark over the committed `eval/` corpus (see `eval/README.md`). `validate`
    cross-checks every label (paths, note ids, H1–H3 headings, group membership, expected counts)
    against the corpus as sync sees it and fails with `contract_violation` on drift. `run`
    validates first, refuses (`config_mismatch`) an index that does not exactly match the corpus,
    and emits a versioned results contract: deterministic retrieval metrics (nDCG@k, per-group
    evidence recall@k, complete@k, MRR) overall and per category/slice. `--stage synthesis` adds
    abstention scoring, citation group coverage, and gold/forbidden fact checks via an LLM judge.
    Always run it against a dedicated `--chroma-path`, never the live-vault index.
- **Note mutations** — `create-note`, `read-note`, `edit-note`, `merge-frontmatter`, `add-links`,
  `insert-related`, `move-note`, `rename-note`, `open-note` (all `./bin/vault-spider <command>`).
  - Executed through the official Obsidian CLI; **the Obsidian app must be running** (macOS only).
    Vault resolution is flags > `config.yaml` > the active vault. Obsidian's registry bridges the
    read path's `vault.root` filesystem path to the mutation backend's vault name and fails with
    `config_mismatch` if configured paths/names disagree or the root is unregistered. Per-command
    `--vault` is the explicit escape hatch: it rejects empty names, validates registered names
    when the registry is readable, and skips only the root-agreement guard; `--binary` also
    overrides config.
  - Every mutating command accepts `--dry-run`: it computes and returns exactly what would change
    with `meta.dry_run: true` and makes no backend mutation calls.
  - `edit-note --edits '[{"old_text":"...","new_text":"...","occurrence":1}]'` edits note
    bodies only. Dry-run returns a rendered unified `diff` plus `expected_sha256`; a real apply
    requires that hash and performs an Obsidian-side compare-and-write against the entire raw note.
    Any body/frontmatter/plugin change after preview fails with `contract_violation`. An omitted
    `occurrence` requires exactly one match, and overlapping operations are rejected. When
    `obsidian.manage_updated: true`, dry-run/apply diffs also render the `updated` value that Vault
    Spider proposes/writes; with the default false, the plugin-owned timestamp is not presented as
    a Vault Spider change.
  - `create-note --auto-id` mints `id` (ULID) and `created`/`updated` (the same timestamp,
    formatted per `timestamps.policy`) for whichever of the three are missing from
    `--frontmatter`; explicit values always win. Templater does not fire on CLI-created notes,
    so prefer `--auto-id` over minting these fields by hand.
  - Contract enforcement: `id`/`created`/`provenance` are immutable once set
    (`contract_violation`); empty
    optional fields (`""`, `[]`, `null`) are refused; creates/moves/renames fail with
    `already_exists` rather than overwrite; `add-links`/`insert-related`/alias patches are
    idempotent. `updated` is left to the modified-date plugin unless
    `obsidian.manage_updated: true`. Timestamps are written untyped, following
    `timestamps.policy`.
  - Path arguments (`--path`, `--to`, `--name`) are validated as clean vault-relative POSIX
    paths — absolute paths, backslashes and `.`/`..` segments are `invalid_arguments` before the
    backend is invoked; link targets must be plain note names (no `[[`/`]]`/newlines). Every
    envelope carries `meta.backend: "obsidian-cli"`.
- `uv run streamlit run scripts/streamlit_app.py`
  - Streamlit UI: Retrieve (mode + granularity selectors), Synthesize, Notes browser.
- `./bin/vault-spider-mcp [--transport stdio|streamable-http] [--host <host>] [--port <port>]`
  - MCP server exposing stats, sync, retrieval, synthesis, lint, enrichment, note reads, and safe
    mutations. Defaults to `stdio` for local clients such as Claude Desktop. Streamable HTTP serves
    `/mcp` for remote clients such as ChatGPT; it binds to `127.0.0.1:8000` by default and has no
    built-in application authentication. Mutation tools default to `dry_run: true`.
- `uv run pytest`
  - Network-free test suite (uses a fake provider; no API key required).

The complete Obsidian-side prerequisite and plugin contract is in
`docs/obsidian-setup.md`; `scripts/setup_obsidian.py` can dry-run or apply it without replacing
unrelated vault settings.

All CLI output is a single JSON envelope on stdout: `{"ok": bool, "action", "result", "meta"}` on
success, `{"ok": false, "action", "error": {"type", "message", "details"}}` on failure (exit 1).
This holds for *every* failure: argparse errors (bad flags, missing/unknown commands) are
converted to `invalid_arguments` envelopes rather than printing usage text.
Error types: `invalid_arguments`, `index_empty`, `provider_error`, `not_found`, `internal_error`,
`obsidian_not_running`, `backend_error`, `already_exists`, `ambiguous_target`,
`config_mismatch`, `contract_violation`. The schema (`vault-spider schema`) is `version: 2` — the version where the
mutation commands were merged in. In the schema, `mutates_state` is always a boolean ("can this
command write?"); the optional `mutates` string qualifies what and when.

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

The `vault_spider` package is layered:

1. `vault_spider/corpus/`
   - `frontmatter.py` — YAML frontmatter split, tag normalization, datetime coercion.
   - `identity.py` — note id resolution (frontmatter `id`/ULID, else path hash).
   - `loader.py` — `load_notes(root)` → `Note` dataclasses (skips `#ignore`/`#secret` in the
     body or frontmatter `tags`, non-UTF-8 files, `.trash/`, `.obsidian/`, `Templates/`, every
     hidden directory — Obsidian never indexes dot-folders like `.git` — and
     Excalidraw drawings — `*.excalidraw.md` / `excalidraw-plugin` frontmatter — whose bodies are
     compressed drawing data rather than prose).
   - `chunker.py` — deterministic `split_sections` plus `document_text` / `section_text`.

2. `vault_spider/index/`
   - `store.py` — `IndexStore` with `sync(root, reset)`; maintains the Chroma collection and two
     in-memory BM25 indexes (document + section), diffing by `note_id` + `content_hash`.
   - `reader.py` — read-only Chroma access for the Notes UI.

3. `vault_spider/retrieval/`
   - `fusion.py` — pure RRF / z-score-sigmoid / min-max fusion.
   - `searcher.py` — `Searcher.hybrid_search` (embeddings + BM25 + fusion + optional rerank +
     recency); `fast`/`thorough` modes, `document`/`section`/`mixed` granularity.
   - `evidence.py` — builds the retrieval output contract (candidate objects + deterministic `why`).

4. `vault_spider/synthesis/`
   - `answer.py` — `synthesize()` turns a retrieval contract into a cited answer with abstention;
     robust JSON parsing / truncation repair. Malformed model output fails closed: a non-boolean
     `abstained` or non-string `answer` is treated as an abstention, never presented as grounded.

5. `vault_spider/compounding/`
   - `distill.py` — `save_distilled_note()` for `synthesize --save`.
   - `lint.py` — `lint_vault()` read-only health checks.
   `vault_spider/enrich/planner.py` — `plan()` enrichment planner (read-only; proposes, never mutates).

6. `vault_spider/evaluation/`
   - `dataset.py` — golden-dataset loading and label validation (`eval_schema_version: 1`;
     manifest + JSONL queries cross-checked against the corpus via `load_notes`/`split_sections`).
   - `runner.py` — benchmark execution and scoring (`results_schema_version: 1`): graded
     (path, heading) label matching, required-evidence groups, retrieval aggregates and
     breakdowns, plus the synthesis-stage abstention scoring and LLM fact judge.

7. `vault_spider/obsidian/`
   - `backend.py` — invocation layer for the official Obsidian CLI: binary discovery, vault
     targeting, noise stripping, error mapping (`obsidian_not_running`, `not_found`, ...), and the
     atomic compare-and-write primitive used by guarded body edits.
   - `notes.py` — the mutation commands: dry-run, no-op detection, collision safety, ambiguity
     rejection, idempotent link/alias merging, `id`/`created` immutability. Uses its own minimal
     untyped frontmatter parser on purpose (the YAML-typed `corpus/frontmatter.py` would not
     round-trip values faithfully).

8. `vault_spider/llm/openrouter.py` — embeddings, rerank, and chat via OpenRouter. Responses are
   strictly validated (index coverage, duplicate detection, dimensions, finite values); anything
   malformed raises `OpenRouterError` (`provider_error`) instead of misaligning the index.

9. `vault_spider/cli.py` + `vault_spider/envelope.py` — the JSON CLI, envelope helpers, and `CliError`.

10. `vault_spider/mcp_server.py` — FastMCP adapter over the JSON CLI. Each call runs in an isolated
    subprocess so CLI validation/contracts remain authoritative and concurrent mutation calls do not
    share the Obsidian backend's process state. Supports stdio and stateless Streamable HTTP.

11. `scripts/streamlit_*.py` — Streamlit pages that import from `vault_spider`.

`tools/backfill.py` — standalone one-time migration that adds `id`/`created`/`updated`
frontmatter to existing notes (dry-run by default; `--apply` to write; never touches bodies).
`uv run tools/backfill.py --root <dir> [--apply] [--report <path>]`. The timestamp policy comes
from `config.yaml` (`timestamps.policy`: `offset_local`, `utc_z`, or `obsidian_local`).

## Paths & Persistence

All of these are configurable in `config.yaml`; the values below are the defaults.

- ChromaDB directory: `./chroma_db/` (`index.chroma_path`)
- Note source: `--root`, then `vault.root`, then the active vault from Obsidian's registry
- Skipped folders: `.trash`, `.obsidian`, `Templates` (`vault.skip_dirs`), all hidden
  directories, plus Excalidraw drawings
- Never-indexed tags: `#ignore`, `#secret` (`vault.ignore_tags`)
- Obsidian mutation backend: binary auto-discovered; vault from `--vault`, then guarded config,
  then the app's active vault; `manage_updated: false` (`obsidian.binary` / `obsidian.vault` /
  `obsidian.manage_updated`)
- Streamlit entrypoint: `scripts/streamlit_app.py`

## Development Notes

- Intra-package imports are absolute (`from vault_spider.corpus import loader`); no import fallbacks.
- `vault_spider/utils.py::validate_vault_relative_path` is the single gate for user-supplied vault
  paths (mutation `--path`/`--to`, `--save-dir`, `enrich --note`) — route any new path argument
  through it rather than validating ad hoc.
- Metadata stored per entry: `note_id`, `granularity`, `title`, `path`, `folder`, `tags`, `date`,
  `created`, `updated`, `note_type`, `content_hash`, `heading`, `line_start`, `line_end`, `source`.
- Retrieval/synthesis JSON contracts are stable; downstream tooling depends on them (see
  `vault-spider schema`).
- The LLM relevance judge was removed; abstention now lives in synthesis.
