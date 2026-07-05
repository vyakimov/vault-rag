# Phase 6 — End-to-end capture flow (integration runbook)

**Executor:** an agent with access to both repos (`vault-rag`, `obsctl`) and the running Obsidian app.
**Prerequisites:** Phases 2, 4, 5 complete (retrieve/enrich CLIs + obsctl). Phase 0/1 strongly recommended first (so metadata invariants are checkable). Phase 3 optional (only step 7 uses it).

## Goal

Prove the full loop — capture → enrich → apply → place — works on realistic inputs, with every step explicit and inspectable. **This phase produces no new code** (except, optionally, a convenience wrapper at the end): it is a test protocol executed with the CLIs built in earlier phases. If a step reveals a bug or contract mismatch between `vault-rag enrich` output and `obsctl` input, fix it in the owning repo before continuing.

## The canonical flow (from the Bear plan — explicit orchestration, no hidden multi-step magic)

```bash
# 0. capture: raw note lands in Inbox/
obsctl create-note --path "Inbox/<name>.md" --content-file raw.txt \
    --frontmatter '{"type": null-or-known, "source_type": "..."}'   # id/created come from... see note below

# 1. plan
vault-rag enrich --root <vault> --note "Inbox/<name>.md" --intent "..." --source-type "..." > plan.json

# 2. apply metadata (from plan.frontmatter_patch)
obsctl merge-frontmatter --path "Inbox/<name>.md" --patch '<plan.frontmatter_patch>' --dry-run   # inspect!
obsctl merge-frontmatter --path "Inbox/<name>.md" --patch '<plan.frontmatter_patch>'

# 3. apply high-confidence links (from plan.link_insertions)
obsctl add-links --path "Inbox/<name>.md" --links '<plan.link_insertions>' --dry-run             # inspect!
obsctl add-links --path "Inbox/<name>.md" --links '<plan.link_insertions>'

# 4. apply related candidates (optional, user-reviewed)
obsctl insert-related --path "Inbox/<name>.md" --targets '<[t.target for t in plan.related_candidates]>'

# 5. canonical placement (only when plan.suggested_path differs AND user agrees)
obsctl rename-note --path "Inbox/<name>.md" --name "<plan.title>"        # if title_changed
obsctl move-note --path "Inbox/<new-name>.md" --to "<folder of plan.suggested_path>"

# 6. re-index
vault-rag sync --root <vault>
```

**Frontmatter-at-capture note:** if Phase 0's Templater setup fires on CLI-created notes, `id`/`created`/`updated` appear automatically — verify in test 1 (this is its own open question; the Phase 0 V4 answer covered edits, not creation). If Templater does NOT fire on CLI creation, obsctl `create-note` calls must include `id` (a fresh ULID) and `created`/`updated` in `--frontmatter`. Determine this in the first test and use the answer for all subsequent tests; record it in the results file.

## Environment for testing

Run against a **copy** of the live vault first: `cp -R "/Users/vy/Documents/Vault 14" /tmp/vault-e2e` — but note obsctl talks to the *running Obsidian's* vault, which is the live one. Two options, in order of preference:
1. Open the copy as a second vault in Obsidian (File → Open another vault → `/tmp/vault-e2e`), and run obsctl with `--vault vault-e2e` and vault-rag with `--root /tmp/vault-e2e`.
2. If multi-vault proves troublesome, run against the live vault but confine every test to a `_e2e-test/` subfolder and clean up completely afterward.
Record which option was used.

## Test matrix (from the Bear roadmap — four input types)

For each test, save: the plan JSON, every dry-run output, and the final note text, into `plans/phase-6-artifacts/<test-name>/` in the vault-rag repo.

### T1 — interview transcript
1. Fabricate a plausible 2-person interview transcript (~100 lines) mentioning 2–3 entities that exist in the vault (pick real note titles from `vault-rag retrieve` output).
2. Run the full flow with `--intent "interview import" --source-type transcript`.
3. Expect: `type: interview` proposed; inline links to the mentioned entities; a sensible title.

### T2 — research dump
1. Take a long unstructured text (concatenate 2–3 paragraphs on a topic covered in the vault, e.g. OpenClaw).
2. Full flow, `--intent "research dump"`.
3. Expect: mostly `related_candidates` rather than inline links; conservative title; likely `Inbox/` placement suggestion.

### T3 — web clipping
1. A short text with a source URL, `--source-type web --source-url https://example.com/article`.
2. Expect: `source_type: web` and `source_url` in the patch.

### T4 — manually drafted note enriched later
1. Create a note by hand (via Obsidian UI or `obsidian create`) with existing frontmatter including `type: idea` and one wikilink already in the body.
2. Run enrich + apply.
3. Expect: existing `type` **not** overwritten (planner drops conflicting proposal); the pre-existing link **not** re-inserted; only genuinely new links/related added.

## Invariant checks (run after EVERY test, script them as a small shell function)

For the test note, via `obsctl read-note`:
- `id` unchanged since creation (capture it at step 0).
- `created` unchanged.
- `updated` behavior matches the Phase 0/5 `manage_updated` policy: bumped after steps 2–4 (content changes), NOT bumped by step 5 (move/rename).
- Unknown frontmatter keys planted at creation (add `custom_probe: x` to T4's note) survive every step.
- After step 5: `obsidian backlinks file="<new name>" format=json` shows the linking notes; the old path yields nothing.
- After step 6: `vault-rag retrieve --query "<distinctive phrase from the note>"` finds the note at its new path.

## Step 7 (optional, if Phase 3 landed) — distilled-note round trip

1. `vault-rag synthesize --query "<question the new T1 note helps answer>" --save --root <vault>`.
2. `vault-rag sync`, then `vault-rag lint --root <vault>` → no `stale_distilled` findings.
3. Edit the T1 note (append a line via `obsctl` or `obsidian append`), wait for `updated` bump, re-run lint → the distilled note IS flagged stale.

## Step 8 (optional) — convenience wrapper

Only after all tests pass: a small `tools/capture.py` in vault-rag that chains steps 0–3 with `--yes/--dry-run` flags, printing each envelope. Explicitly NOT a daemon/skill — a linear script. Skip if time-boxed; the skill layer (Phase 7) can orchestrate the CLIs directly.

## Deliverable

`plans/phase-6-results.md` in the vault-rag repo:
- which vault option was used (copy-as-second-vault vs live-with-test-folder);
- the Templater-on-CLI-create answer;
- T1–T4: pass/fail per invariant check, with artifact folder links;
- any contract mismatches found between enrich output and obsctl input, and where they were fixed;
- cleanup confirmation (test notes/folders removed, `vault-rag sync` run afterward).

## Definition of done

- All four tests pass all invariant checks.
- Artifacts + results file committed to the vault-rag repo.
- Vault(s) left clean.

## Out of scope

- The skill layer (Phase 7).
- Automation/scheduling of capture.
- Processing real inbound data at scale — this phase is about proving the loop, not clearing an inbox.
