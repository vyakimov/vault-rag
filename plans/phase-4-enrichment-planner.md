# Phase 4 — Enrichment planner (`vault-rag enrich`)

**Executor:** a coding agent working in `/Users/vy/Documents/Development/vault-rag`.
**Prerequisite:** Phase 2 merged (package, retrieval contract, CLI, envelope). Phase 3 not required but its `type` metadata handling should exist if merged.

## Goal

An **app-agnostic** planning module: given raw note text plus optional capture context, retrieve its neighborhood from the corpus and emit a structured *enrichment plan* — proposed title, metadata, links, related notes, placement. It reasons and proposes; it **never mutates anything** (no file writes, no index writes). Applying the plan is obsctl's job (Phase 5/6).

## CLI contract

```bash
vault-rag enrich --root <corpus-dir> (--note <vault-relative-path> | --stdin) \
    [--intent "<free text, e.g. 'interview import'>"] \
    [--source-type transcript|web|pdf|manual] \
    [--source-url <url>] \
    [--title "<known title override>"]
```
- `--note`: enrich an existing note (loaded via `corpus.loader`; frontmatter read for existing metadata).
- `--stdin`: enrich a raw text blob that is not yet a note (title unknown unless `--title` given).
- Exactly one of `--note` / `--stdin` required; both/neither → `invalid_arguments`.
- Output: envelope with the plan JSON as `result`. Requires a non-empty index (`index_empty` error otherwise) and `OPENROUTER_API_KEY`.

## Plan output schema (stable contract — obsctl consumes this)

```json
{
  "input": {"path": "Inbox/raw-transcript.md", "given_title": "...", "intent": "...", "source_type": "transcript"},
  "title": "Interview with leader of Vogquestue",
  "title_changed": true,
  "suggested_path": "Inbox/Interview with leader of Vogquestue.md",
  "frontmatter_patch": {"type": "interview", "source_type": "transcript", "source_url": null, "aliases": []},
  "link_insertions": [
    {"target": "Rose", "target_path": "Research/Rose.md", "confidence": 0.95, "mode": "inline",
     "anchor_text": "Rose", "occurs_at_line": 12}
  ],
  "related_candidates": [
    {"target": "Rose Vogquestue", "target_path": "Research/Rose/Vogquestue.md", "confidence": 0.8,
     "reason": "same condition and household context"}
  ],
  "warnings": [],
  "confidence": "high"
}
```
Field rules:
- `frontmatter_patch`: **only** keys `type`, `aliases`, `source_type`, `source_url`. Omit keys that are unknown (never emit empty strings/arrays — strip them in post-processing). Never propose `id`, `created`, `updated`, `tags`.
- `link_insertions`: only targets that resolve to an **existing** note in the corpus (validate against the loader's title/stem set — same resolution rules as Phase 3 lint). `mode` is always `"inline"` in v1.
- `related_candidates`: same existence validation; these go to a `## Related` section when applied.
- `suggested_path`: advisory only. Policy (from the Bear naming/placement note): if the input came from `--stdin` or lives in an inbox-like folder and a clear destination folder exists among the top retrieval neighbors (≥3 of top 5 neighbors share a folder), suggest `<that-folder>/<title>.md`; otherwise suggest keeping the current location (`suggested_path` = current path, or `Inbox/<title>.md` for stdin input). Never invent new folder names that don't exist in the corpus.
- `confidence`: overall `"high" | "medium" | "low"` (see policy below).

## Algorithm

### 1. Gather retrieval neighborhood (support function, not source of truth)
- Build up to 3 queries: (a) the title (given or first heading or first line, ≤100 chars); (b) the first 300 chars of body; (c) the most frequent 5 non-stopword terms joined (use `utils.tokenize_for_bm25` + `collections.Counter`).
- For each query: `Searcher.hybrid_search(mode="fast", granularity="document", n_results=5)`.
- Merge candidates by `note_id` (keep max score), drop the note being enriched (match by path), keep top 10. These are the "neighbors".

### 2. LLM call (one call, `provider.chat`, temperature 0.2, max_tokens 2048)

System prompt (exact):
```
You are an enrichment planner for a personal markdown knowledge vault.
Given a NOTE and its retrieved NEIGHBORS, propose conservative improvements as JSON.
Rules:
- Propose only what the text clearly supports. When unsure, leave fields out and add a warning instead.
- Only propose links to notes listed in NEIGHBORS.
- type must be one of: interview, reference, research, recipe, journal, transcript, idea, project. Omit if unclear.
- aliases only when the note has an obvious alternate name. Never invent aliases.
- Do not rewrite or summarize the note. You are proposing metadata, links, and a title only.
Return JSON: {"title": str, "type": str|null, "aliases": [str], "source_type": str|null,
 "inline_links": [{"target": str, "anchor_text": str, "confidence": 0.0-1.0}],
 "related": [{"target": str, "confidence": 0.0-1.0, "reason": str}],
 "warnings": [str]}
```
User prompt: `NOTE:\n<title line + body, truncated to 8000 chars>\n\nCONTEXT:\nintent=<intent>, source_type=<source-type>\n\nNEIGHBORS:\n` + one line per neighbor: `- title="<title>" path="<path>" excerpt="<first 200 chars>"`.

Parse with `synthesis.answer.parse_llm_json` (reuse; it handles fences/truncation).

### 3. Deterministic post-processing (this is where safety lives — the LLM is not trusted)

- Drop any `inline_links`/`related` entry whose `target` doesn't resolve to a corpus note (add warning each).
- Enforce the confidence policy:
  - `inline_links` require confidence ≥ 0.9; entries between 0.6 and 0.9 are **demoted** to `related_candidates`; below 0.6 dropped.
  - `related_candidates` require ≥ 0.6.
- Dedupe: an inline link target also in related → keep inline only. Links already present in the note body (search for `[[target` literally) → drop with no warning (already linked).
- `frontmatter_patch` keys: validate `type` against the allowed list; drop invalid with warning. If the existing note already has a `type`, never propose a different one (drop + warning `"note already has type=X"`).
- `anchor_text` must literally occur in the body (case-sensitive); otherwise clear `occurs_at_line`/`anchor_text` and demote that link to `related_candidates` (an inline link with no anchor can't be applied surgically).
- `occurs_at_line`: computed in code (first body line containing `anchor_text`), never trusted from the LLM.
- Title: if the LLM title differs only in case/punctuation from the current one, treat as unchanged. `title_changed` computed in code.
- Overall `confidence`: `high` if ≥1 inline link survived and no warnings; `low` if >3 warnings or the LLM output failed to parse (then also: empty patch, empty links, warning `"planner failed to produce a usable plan"`); else `medium`.

### Never (assert in code, not just prompt)
- No file or index writes anywhere in the module (the module must not import `index.store`'s mutating functions or `open(..., "w")`).
- No auto-creation of notes for entities that don't exist.
- No proposals for `id`/`created`/`updated`/`tags`.

## Module layout

```
vault_rag/enrich/
  __init__.py
  planner.py     # gather_neighbors(), build_prompts(), postprocess(), plan(note_or_text, ctx, store, provider) -> dict
```
CLI wires `vault-rag enrich` to `plan()`.

## Tests (`tests/test_enrich.py`, FakeProvider from Phase 2)

- FakeProvider.chat returns a canned planner JSON including: one valid high-confidence link, one link to a nonexistent note, one 0.7-confidence inline link (→ demoted), one alias, an invalid `type` value.
  Assert: nonexistent dropped w/ warning; demotion happened; invalid type dropped; anchors resolved to line numbers; envelope shape correct.
- Note already containing `[[Rose]]` → that link silently dropped.
- Existing `type` in frontmatter → conflicting proposal dropped with warning.
- `--stdin` path: title falls back to first line; `suggested_path` = `Inbox/<title>.md` when no folder consensus.
- Folder-consensus rule: fake neighbors 4/5 in `Research/` → suggested path in `Research/`.
- Unparseable LLM output → `confidence: "low"`, empty proposals, warning present, exit code still 0 (a low-confidence plan is a valid result, not an error).
- Mutation guard: run `plan()` on a fixture vault, checksum tree before/after — identical.

## Definition of done

- Tests green.
- Manual run on 3 real inputs against `./input/Vault 14`:
  1. a transcript-like note (multi-speaker text) with `--intent "interview import" --source-type transcript`;
  2. a research dump (one of the OpenClaw notes copied to `Inbox/`);
  3. an ambiguous 3-line fragment via `--stdin`.
  Check by eye: plans are conservative, links resolve, the fragment yields low/medium confidence with warnings rather than invented structure. Paste all three plan JSONs into the completion report.

## Out of scope

- Applying plans (obsctl, Phases 5–6).
- Summary/synthesis blocks in plans (deferred, post-v1).
- New-entity note creation.
- Query expansion.
