# Capture & enrichment runbook

The less-frequent, multi-step flows: getting new material into the vault and enriching it. Ordering
and confirmation policy only — the actual planning/mutation logic lives in `vault-rag enrich` and
`obsctl`.

## Frontmatter at capture (important)

Templater does **not** fire on CLI-created notes (verified in Phase 6), so `obsctl create-note`
must supply the universal fields itself. The `update-time-on-edit` plugin then maintains `updated`
on every subsequent edit, so obsctl leaves `updated` alone (`manage_updated: false`).

Contract fields (per `plans/phase-0-results.md`):
- `id` — a 26-char ULID (Crockford base32), immutable.
- `created` / `updated` — ISO 8601, **offset-aware local** (e.g. `2026-07-07T14:30:00+02:00`).

Mint them at capture (python-ulid ships with vault-rag):

```bash
uv run python -c "from ulid import ULID; from datetime import datetime,timezone; import json; \
n=datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'); \
print(json.dumps({'id':str(ULID()),'created':n,'updated':n}))"
```

## Capture

```bash
obsctl create-note --path "Inbox/<name>.md" --content-file raw.txt \
    --frontmatter '{"id":"<ULID>","created":"<now>","updated":"<now>", "source_type":"..."}'
```

Set `source_type` if known at capture; leave `type` out (let enrich propose it). After capture,
offer enrichment.

## Enrich → apply (fixed order)

1. **Plan** (read-only, no mutations):
   ```bash
   vault-rag enrich --root <vault> --note "Inbox/<name>.md" --intent "..." --source-type "..." > plan.json
   ```
   Show the user the plan summary: title, `frontmatter_patch`, links, `suggested_path`, confidence.
   If `confidence: low`, show the warnings and apply **nothing** unless the user insists.

2. **Apply in this order, each `--dry-run` first, then for real on confirmation:**
   ```bash
   obsctl merge-frontmatter --path "Inbox/<name>.md" --patch '<plan.frontmatter_patch>'
   obsctl add-links         --path "Inbox/<name>.md" --links '<plan.link_insertions>'
   obsctl insert-related    --path "Inbox/<name>.md" --targets '<[t.target for t in plan.related_candidates]>'
   ```

3. **Placement** (only if the user agrees to the destination):
   ```bash
   obsctl rename-note --path "Inbox/<name>.md" --name "<plan.title>"        # if plan.title_changed
   obsctl move-note   --path "Inbox/<new-name>.md" --to "<folder of plan.suggested_path>"
   ```
   `suggested_path` is advisory. The destination folder must already exist.

4. **Re-index** when done:
   ```bash
   vault-rag sync --root <vault>
   ```

## Safety reminders

- enrich validates links against the index and gates by confidence; obsctl enforces the data
  contract. Do not second-guess them, but always dry-run and confirm before applying.
- Never propose `id` / `created` / `updated` / `tags` in a patch. Move/rename never change `updated`.
- `contract_violation` / `ambiguous_target` from obsctl → surface verbatim, stop, ask the user.
