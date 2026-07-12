# Phase 8 Results

## Shipped

All planned items shipped in order:

- A1-A2: ruff linting and two-job GitHub Actions CI.
- B1-B8: `SearchParams`, phrase-scan short circuit, lazy BM25, lint cleanup,
  utility cleanup with a note-id golden, safer Streamlit synthesis, common CLI flags after
  subcommands, and accurate `mixed` documentation.
- C1-C4: provider-free `stats`, `sync --dry-run`, retrieval metadata filters and
  `--must-include`, duplicate-title linting, and `lint --fix` backed by shared backfill core.
- D1-D3: section embedding reuse, on-disk query embedding cache, and citation-coverage
  warnings.

Each item is represented by its own commit with its item ID in the subject.

## Verification

- Tests before: 146.
- Tests after: 177.
- `uv run pytest -q`: 177 passed.
- `uv run ruff check .`: passed.
- Schema assertions: `stats`, sync `--dry-run`, retrieve/synthesize filters, and lint `--fix`
  are present; `SCHEMA_VERSION` remains 1.
- `uv run pyright vault_rag`: 44 errors, non-blocking as specified. These remain primarily
  existing Chroma/pandas typing friction; Phase 8 type-job behavior is intentionally unchanged.

## Manual Smoke

The live vault was accessed read-only and all generated state used a temporary directory.

- `sync --dry-run`: passed; reported 532 notes to add and did not embed or mutate the index.
- Full `sync` into a temporary Chroma directory: did not complete within the execution window
  (roughly five minutes) and was terminated before producing a JSON envelope.
- `stats`, tagged retrieval, repeated-query cache verification, and `lint --fix` on a vault copy
  were not run because they depended on that full sync completing.
- The original vault and repository `chroma_db/` were not modified.

## Deviations

- The date-filter test sets deterministic old mtimes for notes without explicit frontmatter
  dates because the loader intentionally stores filesystem mtime as the fallback `date`.
- The full live-corpus smoke sequence was incomplete due to the temporary sync runtime noted
  above. Network-free automated coverage exercises every new feature, including cache miss/hit
  behavior and lint fixes on temporary vaults.
