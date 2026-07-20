---
name: vault
description: >-
  Search, answer from, capture into, and maintain the user's Obsidian vault
  using the vault-spider CLI (hybrid retrieval, cited synthesis, lint,
  enrichment, safe note mutations, provenance-aware trust). Use when the user
  asks what they know or wrote about something, or wants notes found, saved,
  enriched, filed, or vault health checked.
---

# vault

One JSON CLI over the user's Obsidian vault. This file is enough for ~80% of tasks; open a
reference only when you hit its topic:

- [references/commands.md](references/commands.md) — every flag, result contract, and mutation
  detail (edit-note guards, move/rename, frontmatter patch rules).
- [references/capture-and-enrichment.md](references/capture-and-enrichment.md) — the fixed
  capture → enrich → apply workflow.
- [references/eval-and-server.md](references/eval-and-server.md) — the MCP server (if you'd
  rather call tools than a CLI) and the golden-dataset eval commands.

## Ground rules

- Invoke the stable wrapper: `<repo>/bin/vault-spider ...` — never construct `uv run` calls.
- Every command prints **one JSON envelope**: `{"ok": true, "action", "result", "meta"}` or
  `{"ok": false, "action", "error": {"type", "message", "details"}}`. **Check `"ok"`, never exit
  codes.** Quote `error.type: message` verbatim to the user; never paraphrase or work around
  `ambiguous_target`, `config_mismatch`, or `contract_violation`.
- Config (`config.yaml`) supplies the vault root and connection facts — omit `--root` unless the
  user targets a different directory.
- Query commands need `.env` (OpenRouter). Mutations need the **Obsidian app running**
  (`error.type: obsidian_not_running` → ask the user to open Obsidian, don't retry).
- `./bin/vault-spider stats` — cheap "is the index alive?" check; needs no API key.
- `./bin/vault-spider schema` — machine-readable contract for everything (version 2).

## Search and answer

User wants to *find/open* notes → `retrieve`, present candidates as `title — path` plus the
one-line `why`. User asks a *question* → `synthesize`, present the answer with `[[title]]`
citations and any `warnings[]` verbatim.

```bash
# titles / proper nouns / "where did I write X"
./bin/vault-spider retrieve --query "..." --mode fast --granularity document -n 10

# conceptual / multi-note / "what do I know about X"  (escalate here when fast looks off-topic)
./bin/vault-spider retrieve --query "..." --mode thorough --granularity mixed

# question → cited answer (defaults: thorough/mixed)
./bin/vault-spider synthesize --query "..."
```

**Scope with filters, never query-stuffing.** All filters work on both commands:

```
--folder <prefix>      folder or any subfolder
--tag <t>              repeatable; every tag must match
--type <kind>          frontmatter type, exact (recipe, runbook, ...)
--provenance human|reference|llm|distilled
--since/--until <ISO>  compared against updated/date; undated notes drop out
--must-include <term>  repeatable; exact term must appear
```

Empty scope → `not_found: No documents match the required filters`: retry unfiltered and say the
scope matched nothing.

**Abstention**: `abstained: true` → report what's missing, offer a broader retrieve; never pad.
**Warnings**: surface "N sentence(s) lack citations" with the answer; it disqualifies `--save`.

## Provenance — who authored a note's words

Every note carries `provenance`: `human` (typed by the user — most authoritative),
`reference` (imported external content; include `source_url`), `llm` (imported LLM output,
e.g. pasted chats), `distilled` (generated from the vault; regenerable pointer — raw notes win
on conflict). Synthesis applies this trust ordering automatically.

- Set it at **capture time** — it describes how the note entered the vault.
- It is **immutable once set**; never include it in a frontmatter patch on an existing note.
- An agent must never stamp its own output `human`.
- Filter with `--provenance` when the user distinguishes "my notes" from clipped/pasted material.

## Capture

New material the user wants kept → create in `Inbox/`, then offer enrichment (full workflow:
[references/capture-and-enrichment.md](references/capture-and-enrichment.md)).

```bash
./bin/vault-spider create-note --path "Inbox/<Name>.md" --content-file raw.txt \
    --auto-id --frontmatter '{"provenance":"reference","source_url":"https://..."}'
```

`--auto-id` mints `id`/`created`/`updated` — never mint those by hand. Pick provenance by origin:
scraped/clipped → `reference` (+ `source_url`); pasted LLM output → `llm`; the user's own words →
`human`. After any capture batch: `./bin/vault-spider sync` (incremental, cheap; `--dry-run`
previews).

## Mutations — hard rules

All mutations (`create-note`, `edit-note`, `merge-frontmatter`, `add-links`, `insert-related`,
`move-note`, `rename-note`) go through the CLI, never direct file writes. Full contracts:
[references/commands.md](references/commands.md).

1. **Dry-run first, show the result, apply on confirmation.** Every mutating command takes
   `--dry-run`.
2. Body edits: `edit-note --edits '[{"old_text":"...","new_text":"..."}]' --dry-run` returns a
   `diff` and `expected_sha256`; apply the same edits with `--expected-sha256 <value>`. After any
   `contract_violation`, re-read and re-dry-run — never reuse a guard.
3. Metadata edits: `merge-frontmatter --patch '{...}'`. Never patch `id`, `created`, `updated`,
   `tags`, or an existing `provenance`.
4. Adding links to existing prose is always allowed (any provenance): `add-links` wraps existing
   anchor text only; `insert-related` appends to `## Related`. Rewriting prose is only ever done
   at the user's explicit request, via `edit-note`.
5. Move/rename only with explicit user approval of the exact destination.

## Saving answers (distilled notes)

Offer `synthesize --save` only when: confidence high/medium AND ≥2 cited notes AND no
uncited-sentence warnings AND the question is reusable. Ask first. The CLI refuses bad saves
itself (`saved: false` → relay its warning, don't retry) and stamps `provenance: distilled`.
Remind: `sync` afterward indexes it.

## Maintenance

"Vault health / broken links / cleanup" → `./bin/vault-spider lint` (no API key). Lead with the
ranked checks: `dangling_targets` (best next notes to write), `empty_notes` (stubs by inbound
links), then counts. `imported_missing_source` lists reference/llm notes lacking `source_url`.
Fixes are opt-in and the user's call: `lint --fix` (adds *missing* id/created/updated only),
`lint --fix-timestamps` (normalizes to `timestamps.policy`).

**A "missing" note is usually excluded by design**: `#secret`/`#ignore` tags, skipped folders,
or an unsynced recent note (`sync` fixes the last one). Check `config.yaml` before blaming the
index.
