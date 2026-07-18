# vault-spider

Hybrid retrieval, cited answers, health checks, and safe note mutations for an Obsidian vault —
one JSON CLI.

It indexes your Markdown notes into ChromaDB and a BM25 index, fuses the two, optionally reranks,
and answers questions **with citations back to the notes** — or abstains when the notes don't
contain the answer. It also carries the vault's write path: contract-enforcing note mutations
(create, frontmatter patch, link, move, rename) executed through the running Obsidian app. Every
command prints a single JSON envelope on stdout, so it is as usable by an agent as it is by a
human.

Your vault is never committed, and nothing about your particular setup is baked into the source:
paths, folder names and tag conventions all live in a gitignored `config.yaml`.

## Install

```bash
uv sync
cp .env.example .env                # OpenRouter key + models
cp config.yaml.example config.yaml  # optional installation-specific settings
```

Edit `.env` with an [OpenRouter](https://openrouter.ai/keys) key. Set `vault.root` in `config.yaml`
if you do not want to use Obsidian's active vault by default. Then:

```bash
./bin/vault-spider sync            # index the vault (embeds every note; takes a few minutes)
./bin/vault-spider stats
```

## Use

```bash
# Find notes
./bin/vault-spider retrieve --query "wireguard setup" --mode fast

# Answer a question, with citations — abstains rather than guessing
./bin/vault-spider synthesize --query "How did I set up the VPN, and why that way?"

# Vault health
./bin/vault-spider lint --format text
```

`bin/vault-spider` is the stable executable entrypoint for callers that need to whitelist one file.
It forwards argv to the existing `uv run vault-spider` command and locates the project independently
of the caller's working directory. Call it by absolute path from anywhere, for example:

```bash
/path/to/vault-spider/bin/vault-spider schema
```

`vault-spider schema` prints the full machine-readable command and contract schema. All output is
`{"ok": true, "action", "result", "meta"}` on success and `{"ok": false, "action", "error"}` on
failure (exit 1) — **check `ok`, not the exit code.** Every failure is that envelope, including
bad flags and unknown commands — argparse usage text never reaches stdout.

There is also a Streamlit UI:

```bash
uv run streamlit run scripts/streamlit_app.py
```

## MCP server — Claude Desktop and ChatGPT

Vault Spider includes an MCP server with explicit tools for stats, sync, retrieval, cited answers,
lint, enrichment planning, note reads/edits, and the safe Obsidian mutation commands. Mutations
default to `dry_run: true` and still go through the official Obsidian CLI. The server delegates to
the JSON CLI, so MCP responses use the same success and error envelopes documented above.

For a local stdio client such as Claude Desktop, first run `uv sync`, then add this to the client's
MCP configuration (replace the repository path):

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

The stable wrapper can also launch the stdio server from a terminal, independently of cwd:

```bash
/path/to/vault-spider/bin/vault-spider-mcp
```

ChatGPT connects to remote MCP endpoints rather than local stdio processes. Start the Streamable
HTTP transport locally with:

```bash
/path/to/vault-spider/bin/vault-spider-mcp \
  --transport streamable-http --host 127.0.0.1 --port 8000
```

The endpoint is `http://127.0.0.1:8000/mcp`. Use OpenAI's secure MCP tunnel for a server on a
developer machine/private network, or deploy it behind an authenticated HTTPS endpoint, then add
that remote `/mcp` URL as a custom app in ChatGPT developer mode. The built-in HTTP transport has
no application authentication; it binds to loopback by default and must not be exposed directly
to an untrusted network.

Server-level overrides are available when needed:

```bash
vault-spider-mcp --chroma-path /other/chroma_db --collection other_notes
```

## Mutating the vault

All write commands go through the official Obsidian CLI rather than touching files directly, so
wikilinks update on move/rename, unknown frontmatter keys survive patches, and vault plugins fire
exactly as if you had edited in the app. **The Obsidian app must be running** for these commands
(macOS only):

```bash
./bin/vault-spider create-note   --path "Inbox/New Idea.md" --content-file draft.txt \
                              --auto-id --frontmatter '{"type":"idea"}'
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

Vault resolution is explicit flags (`--root`/`--vault`), then `config.yaml`, then Obsidian's active
vault. The read path's `vault.root` is mapped to a vault name through Obsidian's registry so reads
and mutations target the same vault. If configured paths and names disagree, or the configured
root is not registered, the command fails closed with `config_mismatch`. An explicit `--vault`
overrides the config agreement guard, rejects empty names, and is validated against the registry
when it is readable.

Safety properties, enforced in code:

- **Every mutating command takes `--dry-run`**: it computes and returns exactly what would change
  (`changed`, diffs) with `meta.dry_run: true` and makes no backend mutation calls.
- **Body edits are preview-bound**: `edit-note` accepts exact `old_text` → `new_text` operations.
  A dry run returns a rendered unified `diff` and `expected_sha256`; applying requires that hash.
  The hash covers the entire raw note, and Obsidian compares the expected text again immediately
  before writing, so any body, frontmatter, or plugin change after preview fails with
  `contract_violation`. Repeated text requires an explicit 1-based `occurrence`; overlapping edits
  are refused. Frontmatter is never edited by this command except for the configured timestamp
  policy: when `obsidian.manage_updated: true`, the rendered diff includes the exact `updated`
  value Vault Spider proposes or writes. Use `merge-frontmatter` for other metadata.
- **`create-note --auto-id` supplies note identity**: it mints a ULID plus equal `created` and
  `updated` timestamps, formatted according to `timestamps.policy`, for whichever fields are
  absent from `--frontmatter`. Explicit values win. Templater does not run for CLI-created notes,
  so prefer this flag over minting those fields manually.
- **Paths cannot escape the vault**: every path argument (`--path`, `--to`, `--save-dir`, …) must
  be a clean vault-relative POSIX path — absolute paths, backslashes and `.`/`..` segments are
  refused before the backend is invoked, and link targets must be plain note names (no `[[`,
  `]]` or newlines).
- **`id` and `created` are immutable** once set — a patch touching them fails with
  `contract_violation`. They may only be set when absent (i.e. at `create-note`).
- **Empty optional fields** (`""`, `[]`, `null`) in a patch are refused.
- **No silent overwrites**: `create-note`, `move-note` and `rename-note` fail with
  `already_exists` when the destination is taken.
- **Idempotent merging**: `add-links` skips targets already linked, `insert-related` dedupes
  against the existing `## Related` section, alias patches union rather than replace.
- `updated` is left alone by default — a modified-date plugin normally owns it. Set
  `obsidian.manage_updated: true` in `config.yaml` only if no such plugin is active.

## How it works

A note is indexed twice: once whole (`document` granularity) and once per heading-delimited section
(`section`). Retrieval runs BM25 and embedding search over the chosen pool, fuses the rankings
(Reciprocal Rank Fusion by default), optionally reranks the top candidates with a cross-encoder, and
applies an exponential recency boost.

- `--mode fast` skips reranking; `--mode thorough` reranks.
- `--granularity document` searches whole notes; `section` searches sections; `mixed` searches the
  section pool with a cap of 3 sections per note.

Synthesis feeds the top candidates to a chat model under a strict contract: cite every claim, or
abstain. An answer that cites nothing is treated as an abstention, and malformed model output
fails closed — an unparseable verdict abstains rather than presenting an ungrounded answer.

`sync` is failure-safe: existing index entries are deleted only after every new embedding has
been computed and validated, so a transient provider error leaves the current index usable and
the next sync retries cleanly. Provider responses are strictly validated (indexes, dimensions,
finite values) — a malformed response is a `provider_error`, never a silently misaligned index.

## Vault health — `lint`

Read-only by default. It reports what you'd actually act on:

| check | what it finds |
|---|---|
| `dangling_targets` | unresolved `[[links]]`, ranked by how many notes want them — the best notes to write next |
| `empty_notes` | stubs, ranked by inbound links — the most-linked empty note is the most valuable to fill |
| `conflict_copies` | `Note 1.md` sitting beside `Note.md` (Obsidian sync artifacts) |
| `broken_wikilinks` | every unresolved link occurrence |
| `duplicate_ids`, `duplicate_titles` | identity collisions |
| `invalid_timestamps` | unparseable timestamps or values that do not match `timestamps.policy` |
| `orphans` | notes with no links in or out |
| `stale_distilled` | a distilled note whose sources changed after it was written |

Link resolution follows Obsidian: frontmatter links (`parents: "[[Daily Notes]]"`) are real edges,
`aliases` resolve, and `[[diagram.png]]` resolves to an attachment rather than being called broken.

Two opt-in fixers write to the vault:

```bash
./bin/vault-spider lint --fix              # add MISSING id/created/updated (never edits a value)
./bin/vault-spider lint --fix-timestamps   # normalize timestamps to timestamps.policy
```

Timestamp policies are `offset_local` (`2026-07-17T17:32:10+02:00`), `utc_z`
(`2026-07-17T15:32:10Z`), and `obsidian_local` (`2026-07-17T17:32:10`). Use
`obsidian_local` when `created`/`updated` are Obsidian **Date & time** properties and localized,
human-friendly rendering is preferred. It stores local wall time without an offset, matching
Obsidian's native property format. Timestamp normalization preserves each note's filesystem
modification time, so a formatting migration does not make the corpus appear newly edited.

## Compounding

- `synthesize --save` persists a high-confidence, well-cited answer as a **distilled note**
  (`type: distilled`) under `vault.distilled_dir`. Distilled notes are regenerable pointers to their
  sources — raw notes always win on conflict, and `lint` flags a distilled note as stale once a
  source outranks it in age.
- `enrich` is a read-only **planner**: given a note, it retrieves the neighbourhood and proposes a
  title, a frontmatter patch, inline links and a folder. It never mutates anything; apply a plan
  with the mutation commands (`merge-frontmatter`, `add-links`, `insert-related`, then
  `rename-note`/`move-note`), each dry-run first.

## Configuration

Everything installation-specific is in `config.yaml` (gitignored — see `config.yaml.example`):
vault root, skipped folders, never-indexed tags, the distilled folder, the Chroma path, the
timestamp policy, and the Obsidian connection facts for the mutation commands (`obsidian.binary`,
`obsidian.vault`, `obsidian.manage_updated`). The file is optional: root resolution is an explicit
`--root`, then `vault.root`, then the active vault from Obsidian's registry; mutations similarly
use an explicit `--vault`, then guarded config, then the active vault. Secrets stay in `.env`.

For the required Obsidian desktop/CLI settings, the timestamp plugin's exact configuration, and an
idempotent installer, see [docs/obsidian-setup.md](docs/obsidian-setup.md).

Notes carrying `#secret` or `#ignore` (in the body or in frontmatter `tags:`) are **never indexed** —
they stay in Obsidian but never reach the vector store or an LLM. Excalidraw drawings are skipped
too: they are `.md` files whose bodies are compressed drawing data, not prose.

## Development

```bash
uv run pytest    # network-free; uses a fake provider, no API key needed
```

The package is layered: `corpus/` (load, parse, chunk) → `index/` (Chroma + BM25) → `retrieval/`
(fusion, search, evidence) → `synthesis/` (cited answers) → `compounding/` (distill, lint) →
`obsidian/` (the mutation backend). The read path works on files directly; the write path goes
through the Obsidian app — that boundary is deliberate. `AGENTS.md` has the full architecture.
