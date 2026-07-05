# Phase 7 — Skill layer (thin orchestration)

**Executor:** a coding agent with access to the vault-rag repo and the user's Claude Code setup (`~/.claude/skills/` for personal skills — confirm the exact skills directory with the user before writing; project-scoped alternative is `.claude/skills/` inside a repo, but this skill spans repos and belongs at user level).
**Prerequisites:** Phases 2–6 complete and verified. Every capability the skill orchestrates already exists as a JSON CLI (`vault-rag`, `obsctl`, `obsidian`). **If any CLI behavior is missing, fix it in the owning repo — never implement logic inside the skill.**

## Goal

A Claude Code skill (`vault`) that encodes *when to use which tool* — nothing else. The Bear plan's rule: "the thin orchestration layer on top, not the place where the real logic lives."

## Deliverable

```
~/.claude/skills/vault/
  SKILL.md
```
Single file; no scripts (the CLIs are the scripts). Keep SKILL.md under ~150 lines: skills are loaded into context — brevity is a feature.

## SKILL.md contents (write exactly this structure)

### Frontmatter
```yaml
---
name: vault
description: Search, answer from, and maintain the user's Obsidian vault (Vault 14) using
  vault-rag (retrieval/synthesis/lint/enrich) and obsctl (safe note mutations). Use when the
  user asks what they know/wrote about something, wants notes found, captured, enriched,
  filed, or vault health checked.
---
```

### Section: Tools and preconditions
- `vault-rag` — run from `/Users/vy/Documents/Development/vault-rag` via `uv run vault-rag ...`; needs `.env` (OpenRouter). Corpus root for the live vault: `/Users/vy/Documents/Vault 14` (confirm with the user if the index root differs; `vault-rag schema` describes commands).
- `obsctl` — on PATH; needs the Obsidian app running. All output JSON envelopes: check `"ok"` field, never exit codes.
- `obsidian` — official CLI, read-only use within this skill (`read`, `backlinks`, `unresolved`, `tags`); errors print `Error:` text with exit 0.

### Section: Decision rules (the heart of the skill — encode these verbatim)

**Retrieval depth:**
- Proper nouns, note titles, "where did I write X" → `retrieve --mode fast --granularity document`.
- Conceptual/multi-note questions, "what do I know about X" → `--mode thorough --granularity mixed`.
- Escalate fast→thorough when fast results look off-topic (no title/keyword overlap with the query).

**Results vs answer:**
- User wants to *find/open* notes → `retrieve` and present the candidate list (title, path, why).
- User asks a *question* → `synthesize` and present the answer with citations as clickable note references.
- `synthesize` returns `abstained: true` → tell the user what's missing; offer a broader retrieve. Never pad an abstained answer.

**Saving distilled notes:**
- Offer `--save` only when: answer is confidence high/medium AND cites ≥2 notes AND the user's question looks reusable (research-y, not operational like "what was that command"). Ask the user; never save silently.

**Capture:**
- New material to store → `obsctl create-note` into `Inbox/` (frontmatter per the Phase 6 recorded policy).
- After capture → offer enrichment (below).

**Enrichment (`vault-rag enrich` → obsctl apply):**
- Run enrich; show the plan summary (title, patch, links, path suggestion, confidence).
- Apply only after user confirmation, always dry-run first, in the Phase 6 order: merge-frontmatter → add-links → insert-related → rename/move.
- Plan confidence `low` → show warnings, apply nothing unless the user insists.

**Mutations — hard rules:**
- Every obsctl mutation: `--dry-run` first, show the diff, then apply. Exception: none in v1.
- Never construct frontmatter patches containing `id`, `created`, `updated`, `tags`.
- Move/rename: only with explicit user approval of the exact destination.
- Anything obsctl reports as `ambiguous_target` or `contract_violation` → surface to the user verbatim, do not work around.

**Maintenance:**
- "Vault health / broken links / cleanup" → `vault-rag lint --root <vault>`, summarize counts, list top findings; fixes are user decisions.
- After any batch of mutations or captures → remind about `vault-rag sync` (or run it if the user agreed).

### Section: Output conventions
- Present retrieval hits as `title — path` lines with one-line why.
- Present synthesis answers with citations rendered as `[[title]]` references the user can open.
- Quote envelope errors (`error.type: message`) rather than paraphrasing.

## Verification

1. Skill discovery: start a fresh Claude Code session, ask "what do I know about OpenClaw sandboxing?" → skill triggers, runs thorough retrieve + synthesize, answer has citations.
2. "Find my notes about kombucha" → fast retrieve, list presented, no synthesis.
3. "Save that as a note" after a good answer → save flow with confirmation.
4. Capture test: "add this to my vault: <paragraph>" → create-note into Inbox + enrichment offer, dry-runs shown.
5. "Check my vault for broken links" → lint summary.
6. Negative test: ask something the vault can't answer → abstention surfaced honestly.

## Definition of done

- All six verification conversations behave as specified.
- SKILL.md ≤ ~150 lines, contains no business logic (no ranking rules, no YAML manipulation, no path math — only tool selection, ordering, and confirmation policy).
- User has reviewed the decision rules once.

## Out of scope

- New CLI capabilities (fix in the owning repo instead).
- Scheduled/background maintenance (a later `/loop` or cron concern, not this skill).
- Multi-vault support.
