# Phase 5 — obsctl: safe Obsidian mutation CLI

**Executor:** a coding agent creating a **new repo** at `~/openclaw/obsctl`.
**Prerequisites:**
- Phase 0 completed — read `plans/phase-0-results.md` in the vault-rag repo, specifically the answer to V4 ("do CLI writes trigger the modified-date plugin?"). This decides whether obsctl patches `updated` itself (see "updated policy").
- The official Obsidian CLI registered (`obsidian` on PATH) or reachable at `/Applications/Obsidian.app/Contents/MacOS/Obsidian`. The Obsidian **app must be running** for every obsctl command.
- Study `~/openclaw/bearctl/bearctl` first — obsctl copies its structure exactly (single-file Python 3 script, no dependencies outside stdlib, JSON-only stdout, `schema` command, `QuietArgumentParser`, `success`/`failure` envelope).

## Goal

A thin, safe, JSON-only wrapper around the official Obsidian CLI that LLM agents can call deterministically. The official CLI does the heavy lifting (its `move`/`rename` update wikilinks; its `property:set` preserves unknown frontmatter keys). obsctl adds what it lacks: typed JSON errors, dry-run, no-op detection, collision safety, ambiguity rejection, idempotent link/alias merging, and data-contract enforcement.

## Verified backend facts (2026-07-05, Obsidian 1.12.7 — the design depends on these)

1. Errors print `Error: ...` **to stdout with exit code 0**. Detection = parse output lines, not exit codes.
2. Startup noise: output may begin with `Loading updated app package ...` and/or `Your Obsidian installer is out of date...` lines — strip lines matching `^(Loading updated|Your Obsidian installer)` before parsing.
3. `create` on an existing path silently creates `Name 1.md` instead of failing. The `Created: <path>` output line reveals the actual path — compare it to the requested path.
4. `property:set type=datetime` rejects timezone-aware values. Untyped `property:set` stores strings verbatim (verified `...Z` round-trips). → Always set timestamps **untyped**.
5. `file=` resolves names like wikilinks (silently picks one among duplicates). → obsctl only ever passes `path=` to the backend.
6. `property:set` preserves unknown keys, but normalizes YAML style (inline lists → block). Acceptable; do not fight it.
7. `prepend` inserts after frontmatter; `append` appends at end; `delete` trashes by default.
8. `eval code="..."` executes JS in the app context; `app.fileManager.processFrontMatter` works (verified). Multi-statement code needs an async IIFE returning a value.
9. Read commands: `read path=`, `properties path= format=json`, `outline path= format=json`, `backlinks file= format=json`, `links path=`.

## Repo scaffold

```
~/openclaw/obsctl/
  obsctl            # executable single-file python3 script (chmod +x, #!/usr/bin/env python3)
  README.md         # install (symlink to /usr/local/bin), config, usage — mirror bearctl's README
  AGENTS.md         # one-pager for agents: JSON-only, schema command, error types
  tests/test_obsctl.py
```
`git init`, first commit after scaffold.

## Envelope + errors (copy bearctl semantics exactly)

```json
{"ok": true,  "action": "merge-frontmatter", "result": {...}, "meta": {"timing_ms": 123, "backend": "obsidian-cli", "dry_run": false}}
{"ok": false, "action": "merge-frontmatter", "error": {"type": "ambiguous_target", "message": "...", "details": {...}}}
```
Error types: `invalid_arguments`, `obsidian_not_running`, `backend_error`, `not_found`, `already_exists`, `ambiguous_target`, `contract_violation`.

Every **mutating** command's result includes: `changed` (bool), `path` (final vault-relative path), plus command-specific fields. Every mutating command accepts `--dry-run`: compute and return exactly what would change (`changed`, diffs) with `meta.dry_run: true` and **no backend mutation calls**.

## Config

`~/.config/obsctl/config.json` (optional):
```json
{"binary": "/usr/local/bin/obsidian", "vault": "Vault 14"}
```
- Binary discovery order: `--binary` flag → config → `/usr/local/bin/obsidian` → `/Applications/Obsidian.app/Contents/MacOS/Obsidian`. If none exists → `invalid_arguments`.
- `--vault <name>` flag overrides config; when set, every backend call is prefixed with `vault="<name>"` (it must precede the command word in the backend argv).
- **No workflow policy in config** — only connection facts.

## Backend invocation layer (one function)

```python
def backend(args: list[str], timeout: float = 20.0) -> str:
    # subprocess.run([binary, *maybe_vault, *args], capture stdout+stderr text)
    # strip noise lines (fact 2)
    # if remaining output starts with "Error:" -> raise BackendError(message)
    #   message "File ... not found"        -> not_found
    #   anything mentioning "vault"/"connect"/timeout or nonzero exit -> obsidian_not_running (best effort)
    #   else -> backend_error
    # return cleaned stdout
```
If the app is not running the CLI fails; map that to `obsidian_not_running` with the message "Obsidian app must be running".

Helper `read_note(path) -> str` = `backend(["read", f"path={path}"])`. Helper `note_exists(path) -> bool` = read and catch not_found. Helper `write_body(path, content)` for surgical mid-document edits (see add-links/insert-related):
```python
def write_body(path: str, content: str) -> None:
    code = ("(async () => { const f = app.vault.getFileByPath(" + json.dumps(path) + "); "
            "if (!f) return 'NOTFOUND'; await app.vault.modify(f, " + json.dumps(content) + "); return 'OK'; })()")
    out = backend(["eval", f"code={code}"])
    # expect "=> OK"; "=> NOTFOUND" -> not_found
```
(`json.dumps` output is valid JS string syntax — this is the sanctioned way to smuggle arbitrary content through argv. Notes up to several hundred KB are fine within macOS ARG_MAX.)

## Contract enforcement (applies to every command)

- Never set/modify `id` or `created` on an existing note: `merge-frontmatter` rejects patches containing them when the note already has a value (`contract_violation`), and only allows setting them when currently absent.
- Timestamps written untyped (fact 4).
- **updated policy** (from Phase 0 V4): if CLI writes trigger the modified-date plugin → obsctl never touches `updated`. If they don't → every mutating command that changed content also does `property:set path=... name=updated value=<now, contract format>` (untyped) as its last step. Implement both, switched by a config key `"manage_updated": true|false` whose default comes from the Phase 0 answer; document it in README.
- Move/rename never touch `updated` regardless (path-only changes are not semantic edits).

## Commands

### `schema` / `list-actions`
As in bearctl: static JSON describing every action, parameters, error types, `"version": 1`, and `mutates_state` flags.

### `create-note`
```bash
obsctl create-note --path "Inbox/Foo.md" [--content "..."|--content-file f|-] [--frontmatter '{"id": "...", "created": "...", "type": "x"}'] [--dry-run]
```
1. Validate `--path` ends in `.md`, no leading `/`.
2. `note_exists(path)` → `already_exists` error (create-only; **no** reliance on backend behavior — fact 3).
3. Compose full text: frontmatter block (rendered as simple `key: value` lines; lists as block style) + content. obsctl composes the file text itself and calls backend `create path="..." content="..."` with the full text (escape real newlines as `\n` — the backend interprets them; also escape literal backslash-n sequences if present in content by… simpler: since backend converts `\n` to newline, replace `\\` first, then `\n`. Test this escaping explicitly).
4. Parse `Created: <actual-path>` from output; if `actual != requested` (shouldn't happen after step 2, but a race is possible) → delete nothing, report `backend_error` with details.
5. Result: `{"changed": true, "path": "..."}`.
Dry-run: report the exact text that would be written.

### `read-note`
```bash
obsctl read-note --path "Inbox/Foo.md" [--frontmatter-only|--body-only]
```
Result: `{"path", "frontmatter": {...parsed via python yaml? NO — stdlib only...}}`.
Stdlib constraint: parse frontmatter with a minimal parser: split the `---` block, parse `key: value` lines + simple block lists (`- item`). Values kept as strings. Complex YAML (nested maps) → keep raw line under `"_raw"` and include a warning. This is sufficient for the contract fields; document the limitation.
Result: `{"path", "frontmatter": {...}, "body": "...", "raw": "..."}`.

### `merge-frontmatter`
```bash
obsctl merge-frontmatter --path "..." --patch '{"type": "interview", "aliases": ["X"]}' [--dry-run]
```
1. Read note; parse current frontmatter.
2. For each patch key: skip if current value already equals patch value (string compare); `id`/`created` per contract-enforcement rules; `aliases` merge = union preserving existing order, appending new (dedupe case-sensitive).
3. Empty-value patch entries (`""`, `[]`, `null`) → rejected `invalid_arguments` ("never write empty optional fields").
4. Apply each changed key via backend `property:set path=... name=<k> value=<v>` (untyped; lists via `type=list` with comma-joined value? — **No**: for lists use one `property:set ... type=list value="a,b"`? The CLI's list handling was verified for single values only. Safer: apply list-valued keys via `eval` + `processFrontMatter` in one call:
   ```js
   (async () => { const f = app.vault.getFileByPath(<path>); await app.fileManager.processFrontMatter(f, fm => { fm.aliases = <json array>; }); return 'OK'; })()
   ```
   Scalars → `property:set`; lists → `processFrontMatter`. )
5. Result: `{"changed": true|false, "fields_touched": ["type", "aliases"], "skipped": {"id": "already set"}}`.
6. `changed: false` (everything already matched) is a success, not an error.
Dry-run: report per-key `current` vs `proposed`.

### `add-links`
```bash
obsctl add-links --path "..." --links '[{"target": "Rose", "anchor_text": "Rose", "line": 12}]' [--dry-run]
```
Consumes Phase 4 `link_insertions`. For each link:
1. Skip (`already: true`) if body already contains `[[target]]` or `[[target|`.
2. Find the anchor: the given `line` (1-based in body) must contain `anchor_text` outside an existing wikilink and outside code fences; else search the whole body for the first such occurrence; none → per-link result `{"applied": false, "reason": "anchor not found"}`.
3. Replace the **first** occurrence on that line: `anchor_text` → `[[target|anchor_text]]` (or `[[target]]` when `anchor_text == target`).
Apply all replacements to the body in memory, then one `write_body`. Result: per-link outcomes + `changed`.
Idempotent: re-running the same call → all `already/skip`, `changed: false`.

### `insert-related`
```bash
obsctl insert-related --path "..." --targets '["Rose Vogquestue", "Vogquestue"]' [--dry-run]
```
1. Read body. Find `## Related` heading (exact match, case-insensitive, outside code fences). Multiple occurrences → `ambiguous_target` error.
2. Absent → append `\n## Related\n` + bullets at end of note.
3. Present → collect existing bullet targets under it (until next heading or EOF); add only missing targets as `- [[Target]]` lines (dedupe against existing wikilink targets, case-insensitive).
4. One `write_body`. Result: `{"changed", "added": [...], "already_present": [...]}`.

### `move-note` / `rename-note`
```bash
obsctl move-note   --path "Inbox/Foo.md" --to "Research/"          [--dry-run]
obsctl rename-note --path "Inbox/Foo.md" --name "Better Title"     [--dry-run]
```
1. Source must exist (`not_found`). Destination path (computed: folder + filename, or same folder + new name + `.md`) must **not** exist (`already_exists`).
2. Backend `move path=... to=...` / `rename path=... name=...` — these update incoming wikilinks (fact: verified).
3. Parse `Moved:`/`Renamed:` output for the final path. Result: `{"changed": true, "path_before", "path_after", "links_updated_by": "obsidian"}`.
4. No `updated` patching (path-only policy).

### `open-note`
```bash
obsctl open-note --path "..."
```
Backend `open path=...`. Result `{"opened": true}`. (Cosmetic; no safety wrapper needed.)

## Tests (`tests/test_obsctl.py`)

Two layers:
1. **Unit (no Obsidian):** monkeypatch `backend()` with canned outputs. Cover: noise-line stripping; `Error:` → typed errors; create collision pre-check; merge skip/reject logic (id/created, empty values, alias union); add-links anchor resolution/idempotency; insert-related dedupe + ambiguity; dry-run never calls mutating backend paths (record calls in the fake); envelope shape for success/failure.
2. **Live smoke test (script `tests/live_smoke.sh`, run manually with Obsidian open):** against the live vault, in a `_obsctl-test/` folder: create → merge (twice — second must be `changed: false`) → add-links → insert-related (twice) → rename (with a second note linking to it; assert the link text updated via `read-note`) → move → verify `id`/`created` unchanged at every step → delete both via backend `delete` → confirm gone. Print PASS/FAIL per step. This is the Bear safety-note testing matrix.

## Definition of done

- Unit tests green (plain `python3 -m pytest`, stdlib-only script).
- Live smoke test passes against the running Obsidian (all steps PASS, vault left clean).
- `obsctl schema` output documented in README; symlink install instructions verified (`ln -s "$(pwd)/obsctl" /usr/local/bin/obsctl`).
- README records the `manage_updated` decision taken from Phase 0.

## Out of scope

- Destructive operations beyond the test cleanup (no `delete-note` command in v1 — deliberate).
- Filesystem fallback when Obsidian isn't running.
- Batch/transaction semantics (multi-note flows are Phase 6 orchestration).
- Windows/Linux support (macOS only, like bearctl).
