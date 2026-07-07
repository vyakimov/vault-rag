# vault CLIs — command reference

Full flags for the three tools the `vault` skill orchestrates. Each command prints one JSON
envelope: `{"ok": true, "action", "result", "meta"}` or `{"ok": false, "action", "error": {...}}`.
Check `"ok"`, not exit codes. Run `vault-rag schema` / `obsctl schema` for the machine-readable
version.

## vault-rag (read/query/plan; run via `uv run vault-rag`)

```
vault-rag schema
vault-rag sync --root "./input/Vault 14" [--reset]
vault-rag retrieve --query "..." [--mode fast|thorough] [--granularity document|section|mixed] [-n 10]
vault-rag synthesize --query "..." [--mode thorough] [--granularity mixed] [--retrieval file.json]
                     [--n-context 8] [--save --root <dir> [--save-dir Distilled]]
vault-rag lint --root <dir> [--format json|text]
vault-rag enrich --root <dir> (--note <vault-rel-path> | --stdin)
                 [--intent "..."] [--source-type transcript|web|pdf|manual] [--source-url ...] [--title ...]
```

- `retrieve` defaults: `fast` / `document`. `synthesize` defaults: `thorough` / `mixed`.
- `retrieve` result = candidate list, each with `note_id`, `path`, `title`, `heading`, `scores`,
  and a deterministic `why`. `reranker` score is `null` in `fast` mode.
- `synthesize` result = `{question, answer, confidence, abstained, citations[], notes_used[],
  warnings[], retrieval}`. `--save` adds `saved` / `saved_path`.
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
