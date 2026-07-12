# Vault Knowledge System — phase execution specs

Master plan: `~/.claude/plans/go-ahead-and-create-crispy-pizza.md`, mirrored in Bear as
"Obsidian plan - master execution plan" (which links the original 12 "Obsidian plan - *" design notes).

Each file below is a **self-contained spec** written to be executed by a coding agent without
access to the design conversation. Feed an agent exactly one phase file plus repo access.

| Phase | File | Where | Depends on |
|---|---|---|---|
| 0 | `phase-0-data-contract-and-plugins.md` | Obsidian app + live vault | — |
| 1 | `phase-1-backfill.md` | vault-rag repo (`tools/`) | 0 (timestamp policy) |
| 2 | `phase-2-vault-rag-refactor.md` | vault-rag repo | — (independent of 0/1) |
| 3 | `phase-3-compounding-layer.md` | vault-rag repo | 2 |
| 4 | `phase-4-enrichment-planner.md` | vault-rag repo | 2 |
| 5 | `phase-5-obsctl.md` | new repo `~/openclaw/obsctl` | 0 (V4 answer) |
| 6 | `phase-6-capture-flow.md` | both repos + running Obsidian | 2, 4, 5 (3 optional) |
| 7 | `phase-7-skill-layer.md` | `~/.claude/skills/vault/` | 2–6 |
| 8 | `phase-8-hardening-and-features.md` | vault-rag repo | 2–4 + review bug-fix branch merged (see file header) |

Recommended order: **2 → 0 → 1 → 3 → 4 → 5 → 6 → 7** (2 first: highest leverage, no Obsidian-side setup).

Fixed decisions (do not re-litigate inside a phase):
- LLM judge removed entirely (Phase 2); abstention lives in synthesis.
- Raw notes are the source of truth; distilled notes are regenerable derived artifacts (Phase 3).
- All mutation goes through obsctl **except** distilled-note creation (direct write, create-only).
- obsctl wraps the official Obsidian CLI (no REST plugin, no third-party CLI, no fs fallback).
- Timestamps ISO 8601 timezone-aware; UTC `Z` preferred; final policy recorded in `phase-0-results.md`.
- `id` = ULID in frontmatter key `id`, immutable; `created` immutable; relationships in bodies, never frontmatter arrays.

Result files phases must produce: `phase-0-results.md`, `phase-6-results.md`, `phase-6-artifacts/`.
