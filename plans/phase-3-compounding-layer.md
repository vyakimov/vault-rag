# Phase 3 — Compounding layer: distilled notes + lint

**Executor:** a coding agent working in `/Users/vy/Documents/Development/vault-rag`.
**Prerequisite:** Phase 2 merged (the `vault_rag` package, JSON contracts, and CLI exist). Read `plans/phase-2-vault-rag-refactor.md` for the retrieval/synthesis contracts referenced here. Timestamp format comes from `plans/phase-0-results.md` if it exists, else default to UTC `Z`.

## Goal

Two features that let good synthesis output compound back into the vault without creating a second source of truth (this is the deliberately-limited adoption of Karpathy's "LLM wiki" pattern):

1. `vault-rag synthesize --save` — persist a good answer as a **distilled note** in the vault.
2. `vault-rag lint` — a read-only health report over the corpus, including staleness of distilled notes.

**Core invariant (encode in code and prompt):** a distilled note is evidence about its *sources*, never about the world. Raw notes always win on conflict. Distilled notes are regenerable artifacts.

## Feature 1 — `synthesize --save`

### CLI

```bash
vault-rag synthesize --query "..." [--save] [--save-dir "Distilled"] [--root <vault-dir>]
```
- `--save` only has meaning with a live query (not with `--retrieval` replay — reject that combination with `invalid_arguments`).
- `--root` is required with `--save` (the vault directory to write into). Note: this may be the indexed copy (`./input/Vault 14`) during development or the live vault later; the code must not assume.
- `--save-dir` is relative to root; default `Distilled`; created if missing.

### Skip conditions (checked in this order — if any hit, do not write; add a warning to the envelope and still return the answer)

1. `abstained == true` → warning `"not saved: model abstained"`.
2. `confidence == "low"` → warning `"not saved: low confidence"`.
3. `citations` empty → warning `"not saved: no citations"`.
4. Target file already exists → warning `"not saved: <path> already exists"` (create-only; never overwrite, no auto-increment).

### Filename derivation

`slug = question lower-cased → spaces and punctuation runs to single "-" → trimmed to 80 chars → strip leading/trailing "-"`. File: `<save-dir>/<slug>.md`. Empty slug (question was all punctuation) → `invalid_arguments` error.

### Note format (exact)

```markdown
---
id: <str(ULID())>
created: <now, chosen timestamp format>
updated: <same as created>
type: distilled
---
# <original question text>

<answer text verbatim from synthesis output>

## Sources
- [[<title of cited note>]] — <heading if non-empty>: <first 120 chars of the citation excerpt>
- [[<title>]]
```

- One `## Sources` bullet per unique cited note (dedupe by `note_id`; if the same note is cited via several sections, list the first heading only).
- Wikilink target is the cited note's **title** (Obsidian resolves by name). If two cited notes share a title, use the vault-relative path without `.md` as the link target instead (Obsidian's full-path link form) — implement the uniqueness check.
- Body wikilinks in the answer text are left exactly as the model produced (do not inject links into prose).

### Envelope result additions

```json
{"saved": true, "saved_path": "Distilled/what-do-i-know-about-x.md"}
```
or `"saved": false` plus the warning.

### Index integration

- The distilled note is a normal markdown note; the next `vault-rag sync` picks it up. Do **not** auto-sync inside `--save` (keep mutation and indexing decoupled); mention "run vault-rag sync to index it" in `meta`.
- Phase 2's loader already carries frontmatter `type` into metadata (`note_type`). Verify evidence objects expose `"type": "distilled"` for these notes (contract field `type`).

### Synthesis prompt addition

Append one line to the system prompt in `synthesis/answer.py`:
```
Some context notes may be marked type=distilled: these are machine-written summaries of other notes. Treat them as pointers, not primary evidence — when a distilled note conflicts with a raw note, trust the raw note.
```
And in `build_context`, when a candidate has `type == "distilled"`, render its context tag as `<S0 type=distilled ...>`.

## Feature 2 — `vault-rag lint`

### CLI

```bash
vault-rag lint --root <vault-dir> [--format json|text]   # default json, envelope-wrapped
```

Read-only: parses files with `corpus.loader` / `corpus.frontmatter`; **no LLM calls, no writes, no Chroma dependency** (works even if the index is empty). Runs over the same file set as the loader (same skips: `.trash/`, `.obsidian/`, `Templates/`, `#ignore`/`#secret` notes — but DO include ignored notes in a count so the report shows them).

### Checks (each produces a `findings` list; empty list = healthy)

1. **`missing_frontmatter_fields`** — notes lacking `id`, `created`, or `updated`. Entry: `{path, missing: ["id", ...]}`.
2. **`invalid_timestamps`** — `created`/`updated`/`date` values that fail `coerce_datetime`, or parse but are **naive** (no timezone). Entry: `{path, field, value, problem: "naive"|"unparseable"}`.
3. **`duplicate_ids`** — same frontmatter `id` on 2+ notes. Entry: `{id, paths: [...]}`.
4. **`broken_wikilinks`** — body wikilinks that resolve to no file. Wikilink extraction regex: `\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]` applied outside fenced code blocks and outside inline backticks (strip `` `...` `` spans first). Resolution rules (mirror Obsidian): target string matched against (a) note titles / filename stems exactly, (b) case-insensitively, (c) as a vault-relative path with or without `.md`. Unmatched → broken. Entry: `{path, target, line}`.
5. **`orphans`** — notes with zero incoming links AND zero outgoing links (informational; distilled notes excluded since their Sources always link out). Entry: `{path}`.
6. **`stale_distilled`** — for each note with `type: distilled`: parse its `## Sources` section's wikilinks; resolve each to a note; if any source note's `updated` (fallback: resolved `date`) is **newer** than the distilled note's `updated` → stale. Entry: `{path, stale_sources: [{source_path, source_updated, distilled_updated}]}`. Unresolvable source links are reported under `broken_wikilinks` too; here add `{path, warning: "unresolvable source link <target>"}`.

### Report shape (envelope `result`)

```json
{
  "root": "...",
  "notes_scanned": 575,
  "notes_ignored": 12,
  "summary": {"missing_frontmatter_fields": 402, "invalid_timestamps": 3, "duplicate_ids": 0,
               "broken_wikilinks": 60, "orphans": 417, "stale_distilled": 1},
  "findings": {"missing_frontmatter_fields": [...], "invalid_timestamps": [...], ...}
}
```
`--format text`: human summary table + first 20 findings per check (still print via stdout, no envelope).

### Accuracy cross-check (manual, one-time)

The official Obsidian CLI reports `unresolved` (60 on the live vault as of 2026-07-05) and `orphans` (417). After implementing, run `obsidian unresolved total` and `obsidian orphans total` against the same vault and compare with lint's counts. They will not match exactly (Obsidian counts link *instances*, excludes different file types, etc.) — investigate discrepancies until each is **explainable**, and write the explanation in the final report. Lint stays corpus-native; the CLI is only a cross-check.

## Tests

Extend the Phase 2 fixtures (`tiny_vault`):
- `--save` writes a file matching the exact format (parse it back with `split_frontmatter`; assert ULID regex, `type: distilled`, `## Sources` bullets, dedupe).
- Each of the four skip conditions.
- Slug edge cases: long question truncation; punctuation-only question errors.
- Duplicate-title citation → path-based link target.
- lint: one fixture note per finding type; assert each check fires exactly where expected and nowhere else.
- Wikilink extraction: alias links `[[A|b]]`, heading links `[[A#h]]`, link inside code fence (ignored), link inside backticks (ignored).
- stale_distilled: distilled note with an older `updated` than its source → flagged; equal timestamps → not flagged.
- lint makes zero writes (checksum the fixture tree before/after).

## Definition of done

- Tests green.
- On the dev corpus (`./input/Vault 14`): `vault-rag synthesize --query "<a question with good coverage, e.g. about OpenClaw>" --save --root "./input/Vault 14"` produces a valid distilled note; `vault-rag sync` indexes it; a follow-up `retrieve` can find it and its evidence carries `"type": "distilled"`.
- Re-running the same `--save` refuses with the exists warning.
- `vault-rag lint --root "./input/Vault 14"` runs, counts cross-checked against `obsidian unresolved`/`orphans` with explainable differences.
- A deliberately staled distilled note (manually bump a source's `updated`) is flagged.

## Out of scope

- LLM contradiction scanning (`lint --deep` — deferred).
- Auto-regeneration of stale distilled notes (lint only flags).
- Writing into the live vault (dev happens against the repo copy; live-vault use is the user's call).
- obsctl integration for note creation (revisit in Phase 6).
