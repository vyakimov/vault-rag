# Obsidian setup for Vault Spider

This is the complete Obsidian-side contract for Vault Spider. Retrieval, indexing, synthesis, and
linting read Markdown directly and do not need Obsidian. Note mutations and MCP write tools use the
official Obsidian CLI, so the desktop app and the settings below matter for those operations.

## Required desktop and CLI setup

1. Install a current Obsidian desktop installer. The official CLI requires the Obsidian 1.12
   installer and Obsidian currently recommends installer version 1.12.7 or newer.
2. In **Settings → General**, enable **Command line interface** and accept the PATH-registration
   prompt. On macOS this normally creates `/usr/local/bin/obsidian`.
3. Register/open the vault in Obsidian. `config.yaml` `vault.root` and `obsidian.vault` must refer
   to that same registered vault; Vault Spider fails closed with `config_mismatch` otherwise.
4. Keep Obsidian running while using `create-note`, `read-note`, `edit-note`, frontmatter/link
   mutations, move/rename/open, or any corresponding MCP tool.

Reference: [official Obsidian CLI documentation](https://help.obsidian.md/cli).

## Required core settings

| Location | Setting | Required value | Why |
|---|---|---|---|
| Settings → Files and links | Automatically update internal links | On | `move-note` and `rename-note` rely on Obsidian to update incoming wikilinks. |
| Core plugins | Properties | On | Gives `created` and `updated` native Date & time rendering/editing. |
| `.obsidian/types.json` | `created` | `datetime` | Makes the type consistent across the whole vault. |
| `.obsidian/types.json` | `updated` | `datetime` | Makes the type consistent across the whole vault. |

The matching Vault Spider configuration is:

```yaml
timestamps:
  policy: obsidian_local

obsidian:
  manage_updated: false
```

`obsidian_local` writes `YYYY-MM-DDTHH:mm:ss`, the format Obsidian documents for Date & time
properties. Obsidian displays it using the operating system's regional date/time format.

## Community plugin

Recommended and installed by the setup script:

- **Update time on edit**
- Plugin id: `update-time-on-edit`
- Canonical repository: <https://github.com/beaussan/update-time-on-edit-obsidian>

Required settings:

| Plugin setting / JSON key | Value | Reason |
|---|---|---|
| Date format / `dateFormat` | `yyyy-MM-dd'T'HH:mm:ss` | Matches `timestamps.policy: obsidian_local` and Obsidian's Date & time type. |
| Enable created time / `enableCreateTime` | `false` | `create-note --auto-id` owns initial `created` and `updated`; filesystem ctime is not the note contract. |
| Updated property / `headerUpdated` | `updated` | Matches Vault Spider's contract field. |
| Created property / `headerCreated` | `created` | Matches Vault Spider's contract field. |

`minMinutesBetweenSaves` is a throttle, not a compatibility requirement. The installer uses `4`
for a fresh configuration and preserves an existing value unless `--min-update-minutes` is passed.
Ignored folders, hash-cache data, and all unrelated plugin settings are preserved.

The alternative is to omit this plugin and set `obsidian.manage_updated: true`. Vault Spider will
then stamp `updated` itself for its own content/frontmatter mutations, but edits made manually or by
other plugins will not be covered. Do not enable both owners: that causes double writes and can
invalidate a guarded `edit-note` preview.

## Automated setup

Dry-run (default):

```bash
uv run python scripts/setup_obsidian.py --root "/path/to/vault"
```

Install/configure through the official CLI, enable the plugin, and reload Obsidian:

```bash
uv run python scripts/setup_obsidian.py --root "/path/to/vault" --apply
```

If the plugin is already installed and Obsidian is fully closed, configuration can be performed
without launching the app:

```bash
uv run python scripts/setup_obsidian.py --root "/path/to/vault" --apply --configure-only
```

The script is idempotent and JSON-only. It merges only the keys listed above, enables the Properties
core plugin and automatic link updates, and preserves other settings. Before applying, it backs up
existing touched files under `.obsidian/.vault-spider-backups/<UTC timestamp>/`.

The script reports the matching Vault Spider settings but deliberately does not rewrite the
repository's commented `config.yaml`; set those two values there yourself.

Normal `--apply` uses these official CLI operations: disable restricted mode when installation or
enablement requires it, install `update-time-on-edit`, temporarily disable it while its settings are
merged, re-enable it, then reload Obsidian. `--configure-only` cannot install plugin assets and must
only be used while Obsidian is closed, or the running app may overwrite the edited JSON.

## Not required

These may be useful to the human vault workflow but Vault Spider does not depend on them:

- Templater (it does not run for CLI-created notes; use `create-note --auto-id`)
- Daily Notes or Periodic Notes
- Dataview or Bases
- Omnisearch
- Table Editor
- Obsidian Sync

If a template folder exists, add its directory name to `config.yaml` `vault.skip_dirs` so templates
are not indexed as knowledge notes.

## Verification

With Obsidian running:

```bash
obsidian version
obsidian plugins:enabled filter=community versions
obsidian plugin id=update-time-on-edit
./bin/vault-spider read-note --path "Some Note.md"
./bin/vault-spider edit-note --path "Some Note.md" \
  --edits '[{"old_text":"old","new_text":"new"}]' --dry-run
```

Also run `./bin/vault-spider lint`. With `timestamps.policy: obsidian_local`, its
`invalid_timestamps` count should be zero after `lint --fix-timestamps`. Apply that migration only
with a current backup; it preserves note mtimes and never guesses unparseable values.

## Operational caveats

- Obsidian CLI commands can launch/focus the desktop app and require a user session; this is not a
  headless mutation backend.
- The update plugin's throttle means several edits within the configured interval may share one
  `updated` value.
- A plugin-driven frontmatter update between `edit-note --dry-run` and apply intentionally causes
  `contract_violation`; rerun the dry-run and use its new SHA-256 guard.
- Never edit note files directly from integration code. Vault Spider mutations go through Obsidian
  so plugins fire and link maintenance remains correct.
