# Phase 0 results (recorded 2026-07-07)

- Installer version after update: 1.12.7 (app 1.12.7, installer 1.12.7) — already up to date, no reinstall needed.
- CLI registered at /usr/local/bin/obsidian: yes (symlink to `/Applications/Obsidian.app/Contents/MacOS/obsidian-cli`). No fallback path needed.
- Timestamp policy: **offset-aware local** (fallback chosen, not UTC `Z`). Format strings: Templater side `YYYY-MM-DDTHH:mm:ssZ`, modified-date-plugin side `yyyy-MM-dd'T'HH:mm:ssxxx` — both render e.g. `2026-07-07T01:53:57+02:00`. Consistent across both.
- Templater template + ulid.js installed: yes — but in **`999 Templates/`** (not `Templates/`, to match this vault's existing numbered-folder convention). Files: `999 Templates/Default.md`, `999 Templates/Scripts/ulid.js`. Templater settings: `trigger_on_file_creation: true`, folder template `/` → `999 Templates/Default.md`.
- Modified-date plugin: `update-time-on-edit` (id `update-time-on-edit`, by beaussan, v2.4.0). Configured keys: `created` → **disabled** (`enableCreateTime: false`, so Templater exclusively owns `created`, per spec); `updated` → enabled (`headerUpdated: "updated"`). Exclude folder: `999 Templates` (root) and `999 Templates/Scripts`, plus `!attachments`.

## Bug found and fixed during verification

The plugin setup was not actually working when verification began — a real bug, not just misconfiguration:

1. `update-time-on-edit`'s own `dateFormat` setting was corrupted on disk: instead of one copy of `yyyy-MM-dd'T'HH:mm:ssxxx`, `data.json` contained that string **repeated ~366 times** concatenated together. Root cause not fully diagnosed (likely the plugin's custom date-format settings-tab editor duplicating input on save), but reproducible effect understood: `date-fns`'s `format()` treats a corrupted repeated-token format string as valid and dutifully repeats the formatted output, producing a `9KB` `updated` value.
2. The plugin's `ignoreGlobalFolder` list had a trailing-space typo (`"999 Templates "` vs the real folder `"999 Templates"`), so the plugin never actually excluded the Templates folder and ended up stamping its (corrupted) output directly into `999 Templates/Default.md`, clobbering the live `<% tp.file.creation_date(...) %>` Templater tag with static garbage text. Every new note then inherited that garbage verbatim.
3. Fixing the on-disk files while Obsidian was running didn't stick at first — two stale `Default.md` editor tabs held old buffer content and got auto-restored (with their content) by Obsidian's workspace-restore-on-launch, re-clobbering the fix even after `obsidian restart`. Fix required closing those tabs in the GUI, then reapplying: reset `dateFormat` to one clean copy, fixed the folder-exclude typo, restored the template's `updated:` line to a live Templater tag, then `obsidian plugin:reload` for both plugins.
4. Backups of the corrupted files were kept as evidence (not yet deleted): `999 Templates/Default.md.bak-phase0`, `.obsidian/plugins/update-time-on-edit/data.json.bak-phase0`. Worth considering a report to the plugin author (github.com/beaussan) since the duplication bug could resurface.

## V1–V6 results

| # | Result | Evidence |
|---|--------|----------|
| V1 | PASS | Fresh note frontmatter: `id: 01KWWXMN8F987FYZ2GXW9B3YNR`, `created: 2026-07-07T01:53:57+02:00`, `updated: 2026-07-07T01:53:57+02:00`. 26-char ULID, correct format, no blank fields. |
| V2 | PASS | After two real edits (one before, one after the plugin's 4-minute throttle window elapsed): `updated` → `2026-07-07T12:00:12+02:00`; `id`/`created` unchanged. Note: plugin is purely event-driven (no background timer) — `minMinutesBetweenSaves: 4` means it only re-checks the throttle at edit time, so a lone edit inside the window produces no visible change until a *later* edit occurs after the window closes. |
| V3 | PASS | Moved note into `300 Personal/`: `id`, `created`, `updated` all unchanged. Chosen policy confirmed: path-only moves do **not** bump `updated`. |
| V4 | **YES** | CLI writes trigger the plugin — same evidence as V2 (both edits were made via `obsidian append`). No separate test needed; obsctl does **not** need to patch `updated` itself. |
| V5 | PASS | `property:set name=type value=test` added `type: test`; `id`/`created`/`updated` all intact. |
| V6 | PASS | All timestamps offset-aware local (`+02:00`), matches chosen policy; no naive timestamps anywhere. |

Cleanup: test notes moved to trash; `vault-rag sync` not applicable (out of scope for this phase — no vault-rag repo changes were made).
