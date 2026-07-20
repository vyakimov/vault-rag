# Capture & enrichment runbook

The less-frequent, multi-step flows: getting new material into the vault and enriching it. Ordering
and confirmation policy only — the actual planning/mutation logic lives in `vault-spider enrich` and
the mutation commands.

## Frontmatter at capture (important)

Templater does **not** fire on CLI-created notes, so `vault-spider create-note` must supply the
universal fields itself — pass `--auto-id` and the CLI mints them. The `update-time-on-edit`
plugin then maintains `updated` on every subsequent edit, so the CLI leaves `updated` alone
(`obsidian.manage_updated: false`).

Contract fields `--auto-id` mints:
- `id` — a 26-char ULID (Crockford base32), immutable.
- `created` / `updated` — the same "now", formatted by `config.yaml` `timestamps.policy`:
  `offset_local` (default, e.g. `2026-07-07T14:30:00+02:00`), `utc_z`, or
  `obsidian_local` (e.g. `2026-07-07T14:30:00`, for native localized Date & time properties).

Values set explicitly in `--frontmatter` always win; `--auto-id` fills only the missing ones.

## Capture

```bash
./bin/vault-spider create-note --path "Inbox/<name>.md" --content-file raw.txt \
    --auto-id --frontmatter '{"provenance":"...", "source_url":"..."}'
```

Set `provenance` at capture — it records who authored the words and is immutable afterwards:
`human` (typed by the owner), `reference` (imported external content; include `source_url`),
`llm` (imported LLM output, e.g. a pasted chat). Leave `type` out (let enrich propose it).
After capture, offer enrichment.

## Enrich → apply (fixed order)

1. **Plan** (read-only, no mutations; `--root` comes from `config.yaml` unless overridden):
   ```bash
   ./bin/vault-spider enrich --note "Inbox/<name>.md" --intent "..." > plan.json
   ```
   Show the user the plan summary: title, `frontmatter_patch`, links, `suggested_path`, confidence.
   If `confidence: low`, show the warnings and apply **nothing** unless the user insists.

2. **Apply in this order, each `--dry-run` first, then for real on confirmation:**
   ```bash
   ./bin/vault-spider merge-frontmatter --path "Inbox/<name>.md" --patch '<plan.frontmatter_patch>'
   ./bin/vault-spider add-links         --path "Inbox/<name>.md" --links '<plan.link_insertions>'
   ./bin/vault-spider insert-related    --path "Inbox/<name>.md" --targets '<[t.target for t in plan.related_candidates]>'
   ```

3. **Placement** (only if the user agrees to the destination):
   ```bash
   ./bin/vault-spider rename-note --path "Inbox/<name>.md" --name "<plan.title>"        # if plan.title_changed
   ./bin/vault-spider move-note   --path "Inbox/<new-name>.md" --to "<folder of plan.suggested_path>"
   ```
   `suggested_path` is advisory. The destination folder must already exist.

4. **Re-index** when done (incremental — only the touched notes are re-embedded):
   ```bash
   ./bin/vault-spider sync
   ```

## Safety reminders

- enrich validates links against the index and gates by confidence; the mutation commands enforce
  the data contract. Do not second-guess them, but always dry-run and confirm before applying.
- Never propose `id` / `created` / `updated` / `tags` in a patch. Move/rename never change `updated`.
- `contract_violation` / `ambiguous_target` → surface verbatim, stop, ask the user.
