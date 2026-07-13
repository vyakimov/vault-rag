# vault CLIs — command reference

Full flags for the three tools the `vault` skill orchestrates. Each command prints one JSON
envelope: `{"ok": true, "action", "result", "meta"}` or `{"ok": false, "action", "error": {...}}`.
Check `"ok"`, not exit codes. Run `vault-rag schema` / `obsctl schema` for the machine-readable
version.

## vault-rag (read/query/plan; run via `uv run vault-rag`)

`--root` defaults to `config.yaml` `vault.root` everywhere; pass it only when that is unset.

```
vault-rag schema
vault-rag sync [--root <dir>] [--reset | --dry-run]
vault-rag stats                              # index statistics; no API key needed
vault-rag retrieve --query "..." [--mode fast|thorough] [--granularity document|section|mixed] [-n 10] [FILTERS]
vault-rag synthesize --query "..." [--mode thorough] [--granularity mixed] [--retrieval file.json]
                     [--n-context 8] [--save [--root <dir>] [--save-dir Distilled]] [FILTERS]
vault-rag lint [--root <dir>] [--format json|text] [--fix] [--fix-timestamps]
vault-rag enrich (--note <vault-rel-path> | --stdin) [--root <dir>]
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
  edits a value); `--fix-timestamps` rewrites *naive* timestamps as offset-aware. Timestamp
  format follows `config.yaml` `timestamps.policy`.
- `enrich` result = an enrichment plan (title, `frontmatter_patch`, `link_insertions`,
  `related_candidates`, `suggested_path`, `confidence`, `warnings`). **enrich never mutates** —
  feed its output to obsctl.
- Error types: `invalid_arguments`, `index_empty`, `provider_error`, `not_found`, `internal_error`.
- Env: `OPENROUTER_API_KEY`, `OPENROUTER_EMBEDDING_MODEL`, `OPENROUTER_CHAT_MODEL`;
  optional `OPENROUTER_RERANK_MODEL` (enables reranking in `thorough`).

## obsctl (all vault mutations; Obsidian must be running)

```
obsctl schema | list-actions
obsctl create-note   --path "Inbox/Foo.md" [--content ...|--content-file f|-] [--frontmatter '{...}'] [--dry-run]
obsctl read-note     --path "..." [--frontmatter-only|--body-only]
obsctl merge-frontmatter --path "..." --patch '{"type":"interview","aliases":["X"]}' [--dry-run]
obsctl add-links     --path "..." --links '[{"target":"Rose","anchor_text":"Rose","line":12}]' [--dry-run]
obsctl insert-related --path "..." --targets '["Rose Vogquestue"]' [--dry-run]
obsctl move-note     --path "Inbox/Foo.md" --to "Research/"       [--dry-run]
obsctl rename-note   --path "Inbox/Foo.md" --name "Better Title"  [--dry-run]
obsctl open-note     --path "..."
```

- Every mutating command supports `--dry-run` (returns the diff, makes no backend mutation).
- Error types: `invalid_arguments`, `obsidian_not_running`, `backend_error`, `not_found`,
  `already_exists`, `ambiguous_target`, `contract_violation`.
- `move-note` needs the destination folder to already exist (enrich's `suggested_path` folders do).

## obsidian (read-only within this skill)

```
obsidian read path="folder/note.md"
obsidian backlinks file="Note" format=json
obsidian unresolved total        # broken-link instance count
obsidian orphans total
obsidian tags
```

Strip leading `Loading updated...` / `Your Obsidian installer...` noise lines; treat an `Error:`
line as failure even though exit code is 0.
