# Phase 1 — Backfill tool for existing notes

**Executor:** a coding agent working in the `vault-rag` repo (`/Users/vy/Documents/Development/vault-rag`).
**Prerequisite:** Phase 0 completed; read `plans/phase-0-results.md` for the chosen timestamp format. If that file does not exist, stop and report — do not guess the timestamp policy.
**Deliverable:** `tools/backfill.py` + tests + a successful dry-run report against a copy of the real vault.

## Goal

Every existing note in the vault ends up with `id`, `created`, and `updated` frontmatter, without damaging anything: existing metadata preserved, bodies untouched, provenance recorded externally.

## Non-negotiable rules

1. **Never** overwrite an existing `id`, `created`, or `updated` value.
2. **Never** modify the note body (anything after the closing `---`). Notes without frontmatter get a new frontmatter block prepended; the body must remain byte-identical.
3. **Never** re-serialize existing frontmatter. New keys are inserted as new lines inside the existing block; existing lines are not touched, reordered, or reformatted. (Do not round-trip through `yaml.dump` — it destroys formatting.)
4. Dry-run is the default. Writes happen only with an explicit `--apply` flag.
5. Ambiguous cases are reported for manual review, never auto-resolved (see "Ambiguity rules").
6. The tool must be idempotent: running it twice produces zero proposed changes the second time.

## CLI contract

```bash
uv run tools/backfill.py --root <vault-dir> [--apply] [--report <path>] [--include-glob "*.md"]
```

- `--root` (required): vault directory to scan. Recursive over `*.md`. Skip the `.trash/`, `.obsidian/`, and `Templates/` directories.
- `--apply`: actually write changes. Without it, print what would change and write the report, but touch nothing.
- `--report`: output path for the migration report (default: `backfill-report-<YYYYMMDD-HHMMSS>.json` in the current directory).
- Exit code 0 on success (even with ambiguities — those are report content), 1 on unexpected errors.
- Obsidian should be **closed** while running with `--apply` (tell the user; check is not automatable, just print a warning).

## Dependencies

Add to `pyproject.toml`: `python-ulid` (import as `from ulid import ULID`; `str(ULID())` → 26-char string). Also add `pytest` as a dev dependency if not present.

## Reuse from the existing codebase

- `scripts/vault_ingestion.py::split_frontmatter(raw_text) -> (dict, str)` — parse frontmatter for **reading** values. Note its behavior: returns `({}, raw_text)` when there is no frontmatter.
- `scripts/vault_ingestion.py::coerce_datetime(value)` — for validating/parsing existing timestamp values.
- Do not import from `scripts/` with the try/except dance; `tools/backfill.py` may use `sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))` at the top (this tool predates the Phase 2 package refactor; keep it self-contained).

## Value resolution (precedence chains)

For each note, resolve each missing field by walking its chain top-to-bottom; the first source that yields a value wins. Record the winning source name in the report.

### `id`
1. `frontmatter` — existing `id` key: keep as-is (nothing to do).
2. `legacy_field` — an existing `uid`, `ulid`, or `luid` key: copy its value into a new `id` line. Leave the legacy line untouched (removal is manual, post-review). If the value does not look like a ULID (regex `^[0-9A-HJKMNP-TV-Z]{26}$`), still migrate it but flag `warning: "legacy id is not a ULID"` in the report.
3. `generated` — new ULID via `str(ULID())`.

### `created`
1. `frontmatter` — existing `created` key.
2. `legacy_field` — existing `date` key **only if** it parses via `coerce_datetime` (many notes use `date` as creation marker). Copy the parsed value into `created` (normalized to the chosen timestamp format); leave `date` untouched.
3. `git_first_commit` — if `--root` is inside a git work tree (`git -C <root> rev-parse --is-inside-work-tree` succeeds): `git -C <root> log --follow --diff-filter=A --format=%aI -- <relpath>` (take the last line; fall back to `git log --follow --format=%aI` last line). Confidence: `high` if the repo has >50 commits, else `medium`.
4. `birthtime` — `os.stat(path).st_birthtime` (macOS). Confidence: `medium`.
5. `mtime` — `os.stat(path).st_mtime`. Confidence: `low`.
6. `migration_time` — now. Confidence: `low`, plus warning.

### `updated`
1. `frontmatter` — existing `updated` key.
2. `git_last_commit` — first line of `git -C <root> log --follow --format=%aI -- <relpath>`. Confidence: `high`/`medium` as above.
3. `mtime`. Confidence: `medium`.
4. `migration_time`. Confidence: `low`, plus warning.

All timestamps are normalized to the Phase 0 format (UTC `Z` preferred): parse with `datetime.fromisoformat` (git `%aI` is ISO with offset), convert `.astimezone(timezone.utc)`, format `strftime("%Y-%m-%dT%H:%M:%SZ")`. If Phase 0 chose offset-aware local instead, convert to the local zone and format accordingly — one code path, driven by a `TIMESTAMP_POLICY` constant at the top of the file.

Sanity rule: if resolved `updated` < resolved `created`, set `updated = created` and add a warning.

## Write mechanics (exact)

Given raw file text:

- **Has frontmatter** (starts with `---\n` and has a closing `---` line): find the line index of the closing `---`. Insert the new `key: value` lines immediately **before** the closing `---`, in the order `id`, `created`, `updated` (only the missing ones). Everything else byte-identical.
- **No frontmatter**: prepend exactly `---\n` + new lines + `---\n` + original text. (Do not add a blank line — Obsidian tolerates body directly after the block; preserving original bytes matters more.)
- Values are written as plain scalars, no quotes: `id: 01HSZ...`, `created: 2026-07-05T11:30:00Z`.
- Write with the same encoding read (`utf-8`, `errors="ignore"` on read is what the existing loader does — for backfill, read with `errors="strict"`; if a file fails to decode, skip it with a report warning instead of corrupting it).

## Ambiguity rules — flag for manual review, do not write

A note goes to the report's `manual_review` list (and receives **no writes at all**, even for unambiguous fields) when any of:
- frontmatter exists but `split_frontmatter` fails to parse it (returns `{}` for a file starting with `---\n` that clearly has a block);
- both `id` and a legacy identifier exist with **different** values;
- more than one legacy identifier field present with different values;
- existing `created`/`updated`/`date` value fails `coerce_datetime`;
- duplicate `id` value shared by two or more notes (collect all ids first, then check).

## Report schema (JSON)

```json
{
  "root": "/path/to/vault",
  "ran_at": "2026-07-05T12:00:00Z",
  "apply": false,
  "totals": {"scanned": 575, "changed": 402, "skipped_unchanged": 150, "manual_review": 23},
  "changes": [
    {
      "path": "300 Personal/Link List.md",
      "field": "created",
      "value": "2024-11-02T09:14:33Z",
      "source": "git_first_commit",
      "confidence": "high",
      "warnings": []
    }
  ],
  "manual_review": [
    {"path": "...", "reason": "id and uid disagree", "details": {"id": "...", "uid": "..."}}
  ]
}
```

One `changes` entry per field per note. `source` is one of: `legacy_field`, `git_first_commit`, `git_last_commit`, `birthtime`, `mtime`, `migration_time`, `generated`.

## Console output

Human-readable summary to stdout (the JSON report is the machine artifact): counts per field per source, list of manual-review paths, and — in dry-run — up to 10 example diffs (path + the lines that would be inserted).

## Tests (`tests/test_backfill.py`)

Use pytest with `tmp_path` fixtures; build tiny fake vaults on the fly. No network, no git dependency except where tested (use `subprocess` to `git init` a temp repo for the git-source tests). Cases:

1. Note with no frontmatter → block prepended, body byte-identical, all three fields, `id` matches ULID regex.
2. Note with frontmatter containing `custom: x` and `tags: [a]` → new keys inserted before closing `---`; the `custom` and `tags` lines byte-identical; key order in file: existing lines, then id/created/updated.
3. Note with existing `created` → `created` untouched, only missing fields added.
4. Note with `uid: <ulid>` → `id` gets that value; `uid` line still present; report source = `legacy_field`.
5. Note with `uid` and `id` disagreeing → in `manual_review`, file untouched.
6. Note with unparseable `created: yesterday` → `manual_review`, untouched.
7. Two notes with duplicate `id` → both in `manual_review`.
8. git repo vault: file committed at a known time → `created` uses `git_first_commit` with that timestamp (compare to the commit's ISO time converted to UTC).
9. Idempotency: run apply, then dry-run again → `changed == 0`.
10. Dry-run writes nothing: file mtimes/bytes unchanged after run without `--apply`.
11. `updated < created` case → clamped, warning recorded.
12. `.trash/`, `.obsidian/`, `Templates/` skipped.

## Execution procedure (after implementation)

1. `cp -R "/Users/vy/Documents/Vault 14" /tmp/vault-backfill-test` — work on a copy first.
2. `uv run tools/backfill.py --root /tmp/vault-backfill-test` (dry-run). Inspect report: source/confidence distribution, manual_review list.
3. `uv run tools/backfill.py --root /tmp/vault-backfill-test --apply`, then dry-run again → 0 changes.
4. Spot-check 5 notes in the copy (one from each source type) by eye.
5. Report findings to the user and **stop — do not run `--apply` against the real vault without the user's explicit go-ahead** (Obsidian must be closed for it; sync/iCloud considerations are the user's call).

## Definition of done

- All 12 tests pass (`uv run pytest tests/test_backfill.py`).
- Dry-run + apply + idempotency check clean on the vault copy.
- Report from the copy run saved and summarized for the user.
- Real-vault apply explicitly left as a user decision.

## Out of scope

- Removing legacy `uid`/`ulid`/`luid` lines (manual, after review).
- Adding `type`/`aliases`/`source_type`/`source_url` to any note.
- Touching wikilinks, bodies, or file locations.
- Any vault-rag indexing changes (Phase 2 reads the same files but is independent).
