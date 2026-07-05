# Phase 0 — Data contract + Obsidian plugins

**Executor:** an agent guiding the user through Obsidian app configuration, then running verification commands. Most steps here change settings inside the Obsidian GUI; the agent verifies each step from the command line and records results.

**Repo:** none (this phase touches the live Obsidian vault and app only).
**Live vault:** `/Users/vy/Documents/Vault 14` (verify with the CLI, don't assume).
**Prerequisite for later phases:** Phase 1 (backfill) and Phase 5 (obsctl) depend on the decisions recorded here.

## Goal

Freeze the metadata conventions for the vault and configure Obsidian so every **newly created** note automatically gets correct universal frontmatter, without manual effort.

## The data contract (fixed — do not re-litigate)

Universal frontmatter fields on all notes:

```yaml
id: 01HSZK8M6YJ6Y2M6J0H4K7T6Q3   # ULID, 26 chars Crockford base32, immutable once set
created: 2026-07-05T11:30:00Z     # ISO 8601, timezone-aware, set once, never changed
updated: 2026-07-05T11:45:00Z     # ISO 8601, timezone-aware, bumped on real edits only
```

Rules:
- Preferred timestamp format is UTC with `Z` suffix. **Fallback** (acceptable only if plugin ergonomics force it): offset-aware local time, e.g. `2026-07-05T13:30:00+02:00`. Naive timestamps (`2026-07-05 13:30`) are **never** acceptable. Whichever format is chosen must be recorded (see Deliverable) and used consistently by every later phase.
- Optional fields (`type`, `aliases`, `source_type`, `source_url`) are only added when actually known. **Never** add blank/empty optional fields to templates.
- Relationships live in note bodies as `[[wikilinks]]` and optional `## Related` sections. **Never** create `links:` or `related_notes:` frontmatter arrays.
- `id` is never regenerated. `created` is never overwritten. Path-only moves do not bump `updated` (default policy; record if changed).

## Step 1 — Update the Obsidian installer and register the CLI

Current state (as of 2026-07-05): app 1.12.7 running on installer 1.8.10. The installer is too old for full CLI support and `/usr/local/bin/obsidian` was never created.

1. User downloads the latest installer from https://obsidian.md/download and reinstalls (drag to /Applications; vaults and settings are preserved — this only replaces the app shell).
2. User opens Obsidian → Settings → General → enables "Command line interface" (may already be on; `~/Library/Application Support/obsidian/obsidian.json` will contain `"cli": true`) and completes CLI registration (macOS prompts for admin to create the symlink).
3. Verify:
   ```bash
   ls -la /usr/local/bin/obsidian        # symlink must exist
   obsidian version                       # prints app + installer versions, no "out of date" warning
   obsidian vault                         # prints: name=Vault 14, path=/Users/vy/Documents/Vault 14, file count
   ```
   If the symlink still doesn't exist, fall back to `/Applications/Obsidian.app/Contents/MacOS/Obsidian <command>` for all later verification steps and record this in the deliverable.

Note: all `obsidian` CLI commands require the Obsidian app to be **running**.

## Step 2 — Install and configure plugins

Three responsibilities, three plugins. Install via Settings → Community plugins → Browse (or `obsidian plugin:install id=<id> enable`).

### 2a. Templater (`templater-obsidian`) — owns `id` and `created` at creation time

1. Install and enable Templater.
2. Create the template folder `Templates/` in the vault if missing; set it as Templater's template folder.
3. Enable Templater's **"Trigger Templater on new file creation"** option.
4. Create a Templater **user script** for ULID generation. In Templater settings, set the user scripts folder to `Templates/scripts/`, then create `Templates/scripts/ulid.js` with exactly:
   ```javascript
   function ulid() {
     const ENC = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
     let ts = Date.now();
     let timeChars = "";
     for (let i = 0; i < 10; i++) { timeChars = ENC[ts % 32] + timeChars; ts = Math.floor(ts / 32); }
     let rand = "";
     const bytes = new Uint8Array(16);
     crypto.getRandomValues(bytes);
     for (let i = 0; i < 16; i++) rand += ENC[bytes[i] % 32];
     return timeChars + rand;
   }
   module.exports = ulid;
   ```
5. Create the default note template `Templates/default.md`:
   ```
   ---
   id: <% tp.user.ulid() %>
   created: <% tp.date.now("YYYY-MM-DDTHH:mm:ss", 0, tp.file.creation_date(), "UTC") %>Z
   updated: <% tp.date.now("YYYY-MM-DDTHH:mm:ss", 0, tp.file.creation_date(), "UTC") %>Z
   ---
   ```
   **Important:** verify the rendered output is genuinely UTC. Templater uses moment.js; if the `"UTC"` argument does not work in the installed version, an alternative that always works is `<% moment.utc().format("YYYY-MM-DDTHH:mm:ss") %>Z`. If neither produces UTC, fall back to offset-aware local time: `<% tp.date.now("YYYY-MM-DDTHH:mm:ssZZ") %>` (moment `ZZ` renders `+0200`; prefer `Z` token variant `YYYY-MM-DDTHH:mm:ssZ` which renders `+02:00`) — and record the fallback decision.
6. Configure Templater folder templates: root folder `/` → `Templates/default.md`, so every new note gets the universal fields.

### 2b. Modified-date plugin — owns `updated`

1. Install **"Update time on edit"** (plugin id `obsidian-update-time-on-edit`, by beaussan). If it is not available under that id, search community plugins for "update time on edit" or "modified date" and pick the most popular equivalent; record which.
2. Configure:
   - Property name for update time: `updated`
   - Property name for creation time: `created` — **but enable "only if missing" behavior if the plugin offers it; the plugin must never overwrite an existing `created`.** If the plugin cannot guarantee that, disable its created-time feature entirely (Templater owns `created`).
   - Date format: `YYYY-MM-DDTHH:mm:ss[Z]` with UTC if the plugin supports UTC; otherwise offset-aware local (`YYYY-MM-DDTHH:mm:ssZ` moment format) — must match the Step 2a decision. Same format everywhere.
   - Exclude folder: `Templates/` (so templates themselves don't get stamped).

### 2c. ULID/UID plugin — optional

The Templater user script from 2a already covers `id` generation at creation time. A dedicated ULID plugin is only needed if one is found that reliably adds `id` on creation AND never regenerates it. If in doubt, skip — Templater is sufficient.

## Step 3 — Verification checklist

Run each check; record pass/fail plus evidence in the deliverable. Use `obsidian` CLI (app running).

| # | Check | How | Expected |
|---|---|---|---|
| V1 | New note gets all three fields | In Obsidian, create note `phase0-test`. Then: `obsidian read path="phase0-test.md"` | Frontmatter has `id` (26-char ULID), `created`, `updated`, correct format, no blank optional fields |
| V2 | Editing bumps `updated` only | Type a line in the note, wait for the plugin's write-back (a few seconds), read again | `updated` changed; `created` and `id` identical |
| V3 | Move/rename doesn't touch `created`/`id` | `obsidian move path="phase0-test.md" to="300 Personal/"` then read | `id`, `created` unchanged. Record whether `updated` changed (decide + record the path-only-move policy; default: should NOT change) |
| V4 | CLI writes trigger the modified-date plugin | `obsidian append path="300 Personal/phase0-test.md" content="cli edit"` , wait, read | If `updated` was bumped → record "CLI writes trigger plugin: YES". If not → "NO — obsctl must patch `updated` itself" (Phase 5 depends on this answer) |
| V5 | `property:set` preserves other keys | `obsidian property:set path="300 Personal/phase0-test.md" name=type value=test` then read | All prior keys intact; note that Obsidian may normalize YAML list style — acceptable |
| V6 | Timestamp format sanity | Inspect V1 output | Matches the chosen policy exactly (UTC `Z` preferred); no naive timestamps |

Cleanup: `obsidian delete path="300 Personal/phase0-test.md"` (goes to trash).

Known CLI caveat (do not "fix"): errors print `Error: ...` to stdout with exit code 0; check output text, not exit codes.

## Deliverable

Write the results to `plans/phase-0-results.md` in the vault-rag repo with exactly these recorded decisions:

```markdown
# Phase 0 results (recorded <date>)
- Installer version after update: ...
- CLI registered at /usr/local/bin/obsidian: yes/no (fallback path used: ...)
- Timestamp policy: UTC Z | offset-aware local (format string: ...)
- Templater template + ulid.js installed: yes/no
- Modified-date plugin: <plugin id>, configured keys: created?/updated
- V1..V6 results: pass/fail each, with the frontmatter block from V1 pasted in
- CLI writes trigger modified-date plugin (V4): YES/NO
- Path-only move bumps updated (V3): YES/NO — chosen policy: ...
```

## Definition of done

- Every new note created in Obsidian automatically has `id`/`created`/`updated` in the chosen format.
- All six verification rows recorded with evidence.
- `plans/phase-0-results.md` exists and answers V4 (blocking question for Phase 5).

## Out of scope for this phase

- Backfilling existing notes (Phase 1).
- Any changes to vault-rag code.
- Adding `type`/`aliases`/etc. to templates.
- Creating type-specific templates (may be added later, only when the type is known at capture time).
