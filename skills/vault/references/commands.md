# vault CLI — command reference

Full flags for the commands the `vault` skill orchestrates. Each command prints one JSON
envelope: `{"ok": true, "action", "result", "meta"}` or `{"ok": false, "action", "error": {...}}`.
Check `"ok"`, not exit codes. Run `./bin/vault-spider schema` for the machine-readable version
(`version: 2` — one schema covers query and mutation commands alike).

Error types (shared union): `invalid_arguments`, `index_empty`, `provider_error`, `not_found`,
`internal_error`, `obsidian_not_running`, `backend_error`, `already_exists`, `ambiguous_target`,
`config_mismatch`, `contract_violation`.

## Query & maintenance commands (run via `./bin/vault-spider`)

Vault resolution is explicit flags, then `config.yaml`, then the active Obsidian vault.
`config_mismatch` means config and Obsidian disagree about the vault; surface it verbatim and
tell the user to fix config.

```
./bin/vault-spider schema
./bin/vault-spider sync [--root <dir>] [--reset | --dry-run]
./bin/vault-spider stats                              # index statistics; no API key needed
./bin/vault-spider retrieve --query "..." [--mode fast|thorough] [--granularity document|section|mixed] [-n 10] [FILTERS]
./bin/vault-spider synthesize --query "..." [--mode thorough] [--granularity mixed] [--retrieval file.json]
                           [--n-context 8] [--save [--root <dir>] [--save-dir Distilled]] [FILTERS]
./bin/vault-spider lint [--root <dir>] [--format json|text] [--fix] [--fix-timestamps]
./bin/vault-spider enrich (--note <vault-rel-path> | --stdin) [--root <dir>]
                       [--intent "..."] [--source-type transcript|web|pdf|manual] [--source-url ...] [--title ...]

FILTERS (retrieve & synthesize):
  [--folder <prefix>] [--tag <t>]... [--type <note_type>] [--since <ISO>] [--until <ISO>]
  [--must-include <term>]...
```

- `retrieve` defaults: `fast` / `document`. `synthesize` defaults: `thorough` / `mixed`. `mixed`
  searches the section pool with a 3-sections-per-note cap; it does not mix in document entries.
- Filter semantics: `--folder` matches the folder or any subfolder; `--tag` is repeatable and
  every given tag must be present (case-insensitive); `--type` matches frontmatter `type` exactly;
  `--since`/`--until` compare against `updated` (falling back to `date`) — entries without either
  are excluded; `--must-include` is repeatable and each term must appear as a whole word
  (punctuation-insensitive). Filters that match nothing → `not_found`; a malformed date →
  `invalid_arguments`.
- `sync` is incremental (only changed content is re-embedded). `--dry-run` returns
  `would_add`/`would_update`/`would_delete` path lists without touching the index; it cannot be
  combined with `--reset`.
- `stats` result = `{total_documents, total_entries, section_entries, unique_folders, unique_tags,
  dated_notes, embedding_model}`; fails with `index_empty` before the first sync.
- `retrieve` result = candidate list, each with `note_id`, `path`, `title`, `heading`, `scores`,
  and a deterministic `why`. `reranker` score is `null` in `fast` mode.
- `synthesize` result = `{question, answer, confidence, abstained, citations[], notes_used[],
  warnings[], retrieval}`. `warnings[]` may include "N sentence(s) lack citations". `--save` adds
  `saved` / `saved_path`; it refuses (with a warning, `saved: false`) abstained, low-confidence,
  or citation-less answers and never overwrites an existing note. `--save` cannot be combined
  with `--retrieval` (replay).
- `lint` checks: `missing_frontmatter_fields`, `invalid_timestamps`, `duplicate_ids`,
  `duplicate_titles`, `broken_wikilinks`, `dangling_targets` (aggregated, ranked by link count),
  `empty_notes` (ranked by inbound links), `conflict_copies` (`Note 1.md` beside `Note.md`),
  `orphans`, `stale_distilled`. `--fix` writes only *missing* `id`/`created`/`updated` (never
  edits a value); `--fix-timestamps` normalizes parseable values to `config.yaml`
  `timestamps.policy`. `obsidian_local` produces native local Date & time values without an
  offset; `offset_local` and `utc_z` remain available for offset-aware storage. Normalization
  preserves each note's filesystem mtime.
- `enrich` result = an enrichment plan (title, `frontmatter_patch`, `link_insertions`,
  `related_candidates`, `suggested_path`, `confidence`, `warnings`). **enrich never mutates** —
  apply its output with the mutation commands below.
- Env: `OPENROUTER_API_KEY`, `OPENROUTER_EMBEDDING_MODEL`, `OPENROUTER_CHAT_MODEL`;
  optional `OPENROUTER_RERANK_MODEL` (enables reranking in `thorough`).

## Mutation commands (same CLI; Obsidian app must be running)

```
./bin/vault-spider create-note   --path "Inbox/Foo.md" [--content ...|--content-file f|-] [--frontmatter '{...}'] [--auto-id] [--dry-run]
./bin/vault-spider read-note     --path "..." [--frontmatter-only|--body-only]
./bin/vault-spider edit-note     --path "..." --edits '[{"old_text":"...","new_text":"...","occurrence":1}]'
                              [--expected-sha256 <dry-run-hash>] [--dry-run]
./bin/vault-spider merge-frontmatter --path "..." --patch '{"type":"interview","aliases":["Alias"]}' [--dry-run]
./bin/vault-spider add-links     --path "..." --links '[{"target":"Some Note","anchor_text":"some note","line":12}]' [--dry-run]
./bin/vault-spider insert-related --path "..." --targets '["Some Note"]' [--dry-run]
./bin/vault-spider move-note     --path "Inbox/Foo.md" --to "Research/"       [--dry-run]
./bin/vault-spider rename-note   --path "Inbox/Foo.md" --name "Better Title"  [--dry-run]
./bin/vault-spider open-note     --path "..."
```

- Every mutating command supports `--dry-run` (returns the diff, makes no backend mutation).
- `edit-note` changes body text only. Each operation selects exact `old_text`; without
  `occurrence`, the text must occur exactly once. `occurrence` is 1-based, operations resolve
  against the original body, and overlaps are refused. Dry-run returns a rendered unified `diff`,
  `expected_sha256`, and `proposed_sha256`. A real apply requires the dry-run hash and fails with
  `contract_violation` if the full raw note changed; the backend compares again inside Obsidian
  immediately before writing. Run a new dry-run after any conflict. Use `merge-frontmatter` for
  metadata edits. When `obsidian.manage_updated: true`, the rendered dry-run/apply diff includes
  the `updated` value Vault Spider proposes/writes; otherwise that field remains plugin-owned.
- `create-note --auto-id` mints `id` (ULID) plus `created`/`updated` (same timestamp, formatted
  per `timestamps.policy`) for whichever of the three are missing from `--frontmatter` — always
  prefer it over minting those fields by hand.
- Connection facts come from `config.yaml` (`obsidian.binary`, `obsidian.vault`,
  `obsidian.manage_updated`); `--binary`/`--vault` override per command. Explicit `--vault`
  rejects empty names, validates registered names when possible, and skips the root-agreement
  guard.
- `id`/`created` are immutable once set (`contract_violation`); empty optional fields are
  refused; creates/moves/renames never overwrite (`already_exists`).
- `move-note` needs the destination folder to already exist (enrich's `suggested_path` folders do).
- Move/rename update incoming wikilinks automatically (the backend does it) and never bump
  `updated`.

## obsidian (official CLI; read-only within this skill)

```
obsidian backlinks file="Note" format=json
obsidian unresolved total        # broken-link instance count
obsidian orphans total
obsidian tags
```

Strip leading `Loading updated...` / `Your Obsidian installer...` noise lines; treat an `Error:`
line as failure even though exit code is 0. (For reading a note, prefer `vault-spider read-note` —
it returns parsed frontmatter in the JSON envelope.)
