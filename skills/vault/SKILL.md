---
name: vault
description: >-
  Search, answer from, and maintain the user's Obsidian vault using
  vault-rag (retrieval / synthesis / lint / enrich) and obsctl (safe note
  mutations). Use when the user asks what they know or wrote about something, or
  wants notes found, captured, enriched, filed, or vault health checked.
---

# vault

Thin orchestration over three JSON CLIs. This file encodes **when to use which tool** — it holds
no ranking, YAML, or path logic (that lives in the CLIs). Every CLI prints one JSON envelope;
**check the `"ok"` field, never exit codes.**

## Tools & preconditions

- **`vault-rag`** — run from the repo via `uv run vault-rag ...`; needs `.env` (OpenRouter) and
  `config.yaml` (vault root, skip dirs, distilled dir — see `config.yaml.example`). Read-only
  against the vault except `synthesize --save` and the opt-in `lint --fix*` fixers. The corpus
  root comes from `config.yaml` (`vault.root`), so `--root` can be omitted on every command.
  `vault-rag stats` needs no API key — it is the cheap "is the index alive?" check.
- **`obsctl`** — on PATH; **needs the Obsidian app running.** All vault mutations go through it.
- **`obsidian`** — the official CLI; read-only use here (`read`, `backlinks`, `unresolved`, `tags`).
  Errors print `Error:` text with exit 0.

`vault-rag schema` and `obsctl schema` describe every command; full flags are in
[references/commands.md](references/commands.md).

## Decision rules

**Find notes vs. answer a question**
- User wants to *find or open* notes → `vault-rag retrieve`; present the candidate list.
- User asks a *question* → `vault-rag synthesize`; present the answer with citations.

**Retrieval depth**
- Proper nouns, note titles, "where did I write X" → `retrieve --mode fast --granularity document`.
- Conceptual / multi-note, "what do I know about X" → `--mode thorough --granularity mixed`
  (`mixed` = section pool capped at 3 sections per note; it never returns whole documents).
- Escalate fast → thorough when fast results look off-topic (no title/keyword overlap).

**Scoped queries → filters, not query stuffing** — when the user scopes by place, kind, tag, or
time ("my journal notes from June", "notes tagged #recipe"), keep the query semantic and pass the
scope as filters — they work on both `retrieve` and `synthesize`: `--folder` (prefix match),
`--tag` (repeatable, all must match), `--type`, `--since`/`--until` (ISO dates, compared against
`updated`/`date` — undated notes drop out), `--must-include` (repeatable, exact term required in
the text). An empty scope fails with `not_found: No documents match the required filters` — retry
without filters and tell the user the scope matched nothing.

**Abstention** — if `synthesize` returns `abstained: true`, tell the user what's missing and offer
a broader retrieve. Never pad an abstained answer. An answer that cites nothing is already treated
as an abstention by the CLI.

**Warnings** — read `warnings[]` on every synthesis. Surface "N sentence(s) lack citations" to the
user with the answer; treat it as a reason not to offer `--save`.

**Missing notes are usually by design** — a note the user knows exists but never surfaces is most
likely excluded on purpose: `#secret`/`#ignore` tags, a skipped folder (`vault.skip_dirs`), a
hidden directory, or an Excalidraw drawing. Check `config.yaml` before suspecting the index. A
recently created note just needs `vault-rag sync`.

**Saving distilled notes** — offer `synthesize --save` only when the answer is confidence
high/medium AND cites ≥2 notes AND has no uncited-sentence warnings AND the question is reusable
(research-y, not operational). Ask first; never save silently. The CLI independently refuses to
save abstained, low-confidence, or citation-less answers and never overwrites — on `saved: false`,
relay its warning instead of retrying. After saving, remind that `vault-rag sync` indexes it.

**Capture & enrichment** — new material → capture into `Inbox/`, then offer enrichment. Both are
multi-step and have a fixed apply order and frontmatter policy: follow
[references/capture-and-enrichment.md](references/capture-and-enrichment.md).

**Maintenance** — "vault health / broken links / cleanup" → `vault-rag lint`; summarize counts,
then lead with the ranked checks: `dangling_targets` (the best notes to write next, by how many
notes want them) and `empty_notes` (the most valuable stubs to fill, by inbound links). Fixes are
the user's decisions; the only built-in fixers are `lint --fix` (adds *missing* `id`/`created`/
`updated`, never edits a value) and `lint --fix-timestamps` (naive → offset-aware) — both opt-in.

**Sync hygiene** — sync is incremental and only re-embeds changed content, so it is cheap to run
after any batch of captures or edits (remind the user, or run it if they agree). When unsure what
changed, `sync --dry-run` previews adds/updates/deletes without touching the index.

## Mutations — hard rules

- Every `obsctl` mutation: run with `--dry-run` first, show the diff, then apply on confirmation.
- Never construct a frontmatter patch containing `id`, `created`, `updated`, or `tags`.
- Move/rename only with explicit user approval of the exact destination.
- Anything `obsctl` reports as `ambiguous_target` or `contract_violation` → surface verbatim; do
  not work around it.

## Output conventions

- Retrieval hits: one `title — path` line each with the one-line `why`.
- Synthesis answers: render citations as `[[title]]` references the user can open; append any
  `warnings[]` verbatim.
- Errors: quote `error.type: message` from the envelope rather than paraphrasing.
