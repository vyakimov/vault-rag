# vault-spider

Vault Spider is a JSON command-line tool for an Obsidian vault. It searches your notes, answers
questions with citations, checks vault health, and makes safe edits to notes.

Vault Spider indexes your Markdown notes into ChromaDB and a BM25 index. It combines the two
search methods, and can rerank the results. It answers questions and cites the source notes. If
the notes do not contain an answer, it says so instead of guessing.

Vault Spider can also edit your vault. It creates notes, changes frontmatter, adds links, and
moves or renames notes. These commands go through the running Obsidian app, so they are safe and
reversible.

Every command prints one JSON object to stdout. This makes Vault Spider easy to use from a script
or from an AI agent, not only from a terminal.

Your vault is never committed to this repository. All personal settings — the vault path, folder
names, tag rules — live in a `config.yaml` file that Git ignores.

## Install

```bash
uv sync
cp .env.example .env                # OpenRouter key and model names
cp config.yaml.example config.yaml  # optional: your own settings
```

Add your [OpenRouter](https://openrouter.ai/keys) key to `.env`. If you do not want to use
Obsidian's active vault, set `vault.root` in `config.yaml`.

Then build the index:

```bash
./bin/vault-spider sync            # embeds every note; takes a few minutes the first time
./bin/vault-spider stats
```

## Periodic sync on macOS

You can install a LaunchAgent. It syncs the vault at login and once per hour.

```bash
uv run scripts/setup_launchd.py          # show the plan, change nothing
uv run scripts/setup_launchd.py --apply  # install, load, and run once now
```

A sync of an unchanged vault does nothing: no note is re-embedded. Lint is off by default, because
a full scan every hour is usually not needed. Add `--with-lint` to turn it on. Enrichment never
runs on a schedule — it needs a specific note and a plan the user must review.

See [docs/launchd.md](docs/launchd.md) for the interval, logs, status, and uninstall commands.

## Basic commands

```bash
# Find notes
./bin/vault-spider retrieve --query "wireguard setup" --mode fast

# Answer a question, with citations. Abstains instead of guessing.
./bin/vault-spider synthesize --query "How did I set up the VPN, and why that way?"

# Check vault health
./bin/vault-spider lint --format text
```

Use `bin/vault-spider` as the fixed entry point. It finds the project itself, so you can call it
by its full path from any directory:

```bash
/path/to/vault-spider/bin/vault-spider schema
```

`vault-spider schema` prints the full command and data contract, in JSON.

Every command returns `{"ok": true, "action", "result", "meta"}` on success, or
`{"ok": false, "action", "error"}` on failure (exit code 1). **Always check the `ok` field. Do
not rely on the exit code alone.** Even a bad flag or an unknown command returns this same JSON
shape — the tool never prints plain usage text to stdout.

There is also a Streamlit UI:

```bash
uv run streamlit run scripts/streamlit_app.py
```

### Filters

`retrieve` and `synthesize` accept the same filters. Use them to narrow a search:

| flag | matches |
|---|---|
| `--folder <path>` | the folder, or any of its subfolders |
| `--tag <tag>` | notes with this tag (repeat the flag for more than one tag) |
| `--type <kind>` | the frontmatter `type` field, exactly |
| `--provenance <value>` | the frontmatter `provenance` field: `human`, `reference`, `llm`, or `distilled` |
| `--since <date>` / `--until <date>` | the note's `updated` date, or `date` if `updated` is not set |
| `--must-include <term>` | the exact word must appear in the note (repeat for more terms) |

If a filter matches no notes, the command fails with `not_found`. Retry without the filter and
tell the user the scope was empty.

## Provenance: who wrote each note

Every note carries a `provenance` field. It records who wrote the note's words.

| value | meaning |
|---|---|
| `human` | You typed this note. It is the most trusted kind. |
| `reference` | You imported this from an external source — a paper, a web page, a blog post. |
| `llm` | You imported LLM output — for example, a pasted chat answer. |
| `distilled` | Vault Spider generated this note from other notes. You can regenerate it at any time. |

`synthesize` uses provenance to weigh evidence. It trusts your own notes over imported ones. It
treats `distilled` notes as pointers to their sources, not as primary evidence.

Set `provenance` when you create a note. **Once set, it cannot change through the CLI.** To fix a
wrong value, edit the note's frontmatter directly in Obsidian.

If your vault predates this field, run the backfill tool. It is a dry run by default:

```bash
uv run tools/backfill_provenance.py --root /path/to/vault           # show the plan
uv run tools/backfill_provenance.py --root /path/to/vault --apply   # write the changes
```

The tool guesses `provenance` from existing clues (`type: distilled`, `source_url`, an old
`source_type` value) and defaults to `human` when it finds none. Review notes that contain pasted
LLM output — the tool cannot detect these on its own, so check and relabel them by hand.

## MCP server — Claude Desktop and ChatGPT

Vault Spider includes an MCP server. It exposes tools for stats, sync, search, cited answers,
lint, enrichment planning, note reads and edits, and the safe mutation commands. Mutation tools
default to `dry_run: true`. The server calls the same JSON CLI, so it returns the same success
and error shapes described above.

For a local stdio client such as Claude Desktop, run `uv sync` once. Then add this to the
client's MCP configuration (use your own repository path):

```json
{
  "mcpServers": {
    "vault-spider": {
      "command": "/path/to/vault-spider/.venv/bin/python",
      "args": ["-m", "vault_spider.mcp_server"]
    }
  }
}
```

You can also start the stdio server from a terminal, from any directory:

```bash
/path/to/vault-spider/bin/vault-spider-mcp
```

ChatGPT needs a remote MCP endpoint, not a local stdio process. Start the HTTP transport:

```bash
/path/to/vault-spider/bin/vault-spider-mcp \
  --transport streamable-http --host 127.0.0.1 --port 8000
```

This serves `http://127.0.0.1:8000/mcp`. For a machine on a private network, use OpenAI's secure
MCP tunnel, or put the server behind your own authenticated HTTPS endpoint. Then add that remote
`/mcp` URL as a custom app in ChatGPT developer mode.

**The built-in HTTP transport has no login of its own.** It listens on localhost by default. Do
not expose it directly to an untrusted network.

You can override the index location per server:

```bash
vault-spider-mcp --chroma-path /other/chroma_db --collection other_notes
```

## Agent skill

`skills/vault/` is a skill file for AI agents. It teaches an agent when to search, when to ask
a question, how to set `provenance` on new notes, and how to make safe edits. Point any
skill-aware agent at this folder to give it full, correct use of Vault Spider without re-deriving
the rules from the CLI source.

## Mutating the vault

Write commands go through the official Obsidian CLI. They never touch vault files directly. This
means wikilinks update on move and rename, unknown frontmatter keys survive a patch, and plugins
run exactly as if you had made the change in the app. **The Obsidian app must be running**
(macOS only):

```bash
./bin/vault-spider create-note   --path "Inbox/New Idea.md" --content-file draft.txt \
                              --auto-id --frontmatter '{"type":"idea","provenance":"human"}'
./bin/vault-spider read-note     --path "Inbox/New Idea.md" [--frontmatter-only|--body-only]
./bin/vault-spider edit-note     --path "Inbox/New Idea.md" \
                              --edits '[{"old_text":"first draft","new_text":"revised text"}]' \
                              --dry-run
./bin/vault-spider edit-note     --path "Inbox/New Idea.md" \
                              --edits '[{"old_text":"first draft","new_text":"revised text"}]' \
                              --expected-sha256 '<hash returned by dry-run>'
./bin/vault-spider merge-frontmatter --path "..." --patch '{"type":"idea","aliases":["Alias"]}'
./bin/vault-spider add-links     --path "..." --links '[{"target":"Some Note","anchor_text":"some note","line":12}]'
./bin/vault-spider insert-related --path "..." --targets '["Some Note"]'
./bin/vault-spider move-note     --path "Inbox/New Idea.md" --to "Research/"
./bin/vault-spider rename-note   --path "Inbox/New Idea.md" --name "Better Title"
./bin/vault-spider open-note     --path "..."
```

Vault Spider picks the target vault in this order: an explicit `--root` or `--vault` flag, then
`config.yaml`, then Obsidian's active vault. It maps `vault.root` to a vault name through
Obsidian's registry, so reads and writes always target the same vault. If the configured path and
name disagree, or the root is not registered, the command fails with `config_mismatch`. An
explicit `--vault` skips this check. It still rejects an empty name, and it checks the name
against the registry when the registry is readable.

Safety rules, enforced by the code:

- **Every write command supports `--dry-run`.** It returns exactly what would change (`changed`,
  a diff) and sets `meta.dry_run: true`. It makes no change to the vault.
- **Body edits need a preview first.** `edit-note` takes a list of exact `old_text` →
  `new_text` pairs. A dry run returns a diff and an `expected_sha256` hash. To apply the edit,
  pass that hash back with `--expected-sha256`. The hash covers the whole note. Obsidian checks
  the text again right before writing, so any change since the preview — body, frontmatter, or
  from a plugin — fails with `contract_violation`. If the same text appears more than once, set
  the 1-based `occurrence` you mean. Overlapping edits are refused. `edit-note` does not touch
  frontmatter, except the `updated` timestamp when `obsidian.manage_updated: true` is set. Use
  `merge-frontmatter` for all other metadata changes.
- **`create-note --auto-id` sets note identity for you.** It creates a ULID and matching
  `created`/`updated` timestamps, in the format set by `timestamps.policy`, for any of the three
  fields missing from `--frontmatter`. A value you supply always wins. Templater does not run for
  notes created by the CLI, so prefer `--auto-id` over typing these fields by hand.
- **A path can never leave the vault.** Every path argument (`--path`, `--to`, `--save-dir`, and
  so on) must be a plain vault-relative path. An absolute path, a backslash, or a `.`/`..`
  segment is refused before the command reaches Obsidian. A link target must be a plain note
  name — no `[[`, `]]`, or newline characters.
- **`id`, `created`, and `provenance` cannot change once set.** A patch that touches one of them
  fails with `contract_violation`. You may only set them when they are absent — normally, at
  `create-note`.
- **An empty value in a patch is refused.** This covers `""`, `[]`, and `null`.
- **Nothing is overwritten silently.** `create-note`, `move-note`, and `rename-note` fail with
  `already_exists` if the destination is already taken.
- **Repeated edits do not duplicate work.** `add-links` skips a target that is already linked.
  `insert-related` skips a target already in the `## Related` section. An alias patch adds to
  the existing list instead of replacing it.
- **`updated` is left alone by default.** A modified-date plugin usually owns this field. Set
  `obsidian.manage_updated: true` in `config.yaml` only if no such plugin is active.

## How search and answers work

Vault Spider indexes each note twice: once as a whole note (`document` granularity), and once per
section, split at each heading (`section` granularity). A search runs BM25 and embedding search
over the chosen pool, combines the two rankings (Reciprocal Rank Fusion by default), optionally
reranks the top results with a cross-encoder model, and applies a boost for recent notes.

- `--mode fast` skips the rerank step. `--mode thorough` reranks.
- `--granularity document` searches whole notes. `section` searches sections. `mixed` searches
  sections only, with a limit of 3 sections per note.

`synthesize` sends the top results to a chat model, under one rule: cite every claim, or abstain.
An answer with no citation is treated as an abstention. If the model's output cannot be parsed,
Vault Spider also treats this as an abstention — it never shows an answer it cannot verify.

`sync` is safe to interrupt. It deletes an old index entry only after the new one is built and
checked. If the embedding provider fails partway through, the existing index still works, and the
next sync retries cleanly. Every provider response is checked (index order, vector size, no
invalid numbers). A bad response becomes a `provider_error` — it never corrupts the index.

## Vault health — `lint`

`lint` is read-only by default. It reports the checks you would actually act on:

| check | what it finds |
|---|---|
| `dangling_targets` | links with no target note, ranked by how many notes want them — write these next |
| `empty_notes` | stub notes, ranked by inbound links — the most-linked stub is worth filling first |
| `imported_missing_source` | a `reference` or `llm` note with no `source_url` |
| `conflict_copies` | a file like `Note 1.md` next to `Note.md` — usually a sync conflict |
| `broken_wikilinks` | every unresolved link, listed one by one |
| `duplicate_ids`, `duplicate_titles` | two notes sharing an identity |
| `invalid_timestamps` | a timestamp that cannot be parsed, or does not match `timestamps.policy` |
| `orphans` | a note with no links in or out |
| `stale_distilled` | a distilled note whose sources changed after it was written |

Link checks follow Obsidian's own rules. A frontmatter link (`parents: "[[Daily Notes]]"`) counts
as a real link. An alias resolves to its note. A link like `[[diagram.png]]` resolves to an
attachment file, not a broken link.

Two commands can write to the vault. Both are opt-in:

```bash
./bin/vault-spider lint --fix              # add a MISSING id/created/updated (never changes a value)
./bin/vault-spider lint --fix-timestamps   # rewrite timestamps to match timestamps.policy
```

Vault Spider supports three timestamp formats: `offset_local` (`2026-07-17T17:32:10+02:00`),
`utc_z` (`2026-07-17T15:32:10Z`), and `obsidian_local` (`2026-07-17T17:32:10`). Use
`obsidian_local` when `created` and `updated` are Obsidian's own **Date & time** properties, so
Obsidian renders them in your local format. `--fix-timestamps` keeps each note's file
modification time unchanged, so the fix does not make old notes look newly edited.

## Distilled notes and enrichment

- `synthesize --save` saves a high-confidence, well-cited answer as a **distilled note**, in the
  folder set by `vault.distilled_dir`. A distilled note gets `type: distilled` and
  `provenance: distilled`. It is a regenerable pointer to its sources — a raw note always wins if
  the two disagree. `lint` flags a distilled note as stale once a source note becomes newer than
  it.
- `enrich` is a read-only planner. Given one note, it searches its neighborhood and proposes a
  title, a frontmatter patch, inline links, and a folder. It never sets `provenance` — that value
  depends on how the note entered the vault, not on its content. `enrich` never changes a file.
  Apply its plan with the mutation commands (`merge-frontmatter`, `add-links`,
  `insert-related`, then `rename-note` or `move-note`), each with `--dry-run` first.

## Evaluation

Vault Spider ships a benchmark for its own retrieval and synthesis quality. Two sample corpora
live in the repo: `eval/` (a small, hand-written vault) and `eval-realistic/` (a larger vault
that reads like a real, messy one, with the same kind of notes and typos).

```bash
# check that the labels still match the corpus
VAULT_SPIDER_CONFIG=eval/eval-config.yaml ./bin/vault-spider eval validate --dataset eval

# index the corpus into its own Chroma path — never the live-vault index — then score it
VAULT_SPIDER_CONFIG=eval/eval-config.yaml ./bin/vault-spider sync \
    --root eval/public_vault --reset --chroma-path /tmp/vs-eval
VAULT_SPIDER_CONFIG=eval/eval-config.yaml ./bin/vault-spider eval run \
    --dataset eval --chroma-path /tmp/vs-eval --out results.json
```

`eval run` scores search quality by default: ranking accuracy at different cutoffs, and how many
required facts are found. Add `--stage synthesis` to also score abstention and fact-checking,
using a second model as judge. See [eval/README.md](eval/README.md) and
[eval-realistic/README.md](eval-realistic/README.md) for full detail on each corpus.

## Configuration

`config.yaml` holds every setting specific to your install (Git ignores this file — see
`config.yaml.example`): the vault root, skipped folders, tags that are never indexed, the
distilled-note folder, the Chroma index path, the timestamp format, and the Obsidian connection
settings (`obsidian.binary`, `obsidian.vault`, `obsidian.manage_updated`).

The file is optional. Vault Spider picks the vault root from an explicit `--root`, then
`vault.root`, then Obsidian's active vault. It picks the mutation target the same way, using
`--vault` in place of `--root`. Secrets stay in `.env`, never in `config.yaml`.

See [docs/obsidian-setup.md](docs/obsidian-setup.md) for the required Obsidian desktop and CLI
settings, the exact timestamp-plugin configuration, and an installer that can apply them for you.

A note with `#secret` or `#ignore` — in its body, or in frontmatter `tags:` — is **never
indexed**. It stays visible in Obsidian, but it never reaches the search index or a language
model. Excalidraw drawings are skipped for the same kind of reason: their file body is compressed
drawing data, not text.

## Development

```bash
uv run pytest    # no network calls; uses a fake provider, no API key needed
```

The code is organized in layers: `corpus/` (load and parse notes) → `index/` (Chroma and BM25) →
`retrieval/` (combine and rank search results) → `synthesis/` (build a cited answer) →
`compounding/` (distill and lint) → `obsidian/` (the write path). The read path works on vault
files directly. The write path always goes through the Obsidian app — this separation is
deliberate, not an oversight. See `AGENTS.md` for the full architecture.
