# vault-rag

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
cp config.yaml.example config.yaml  # where your vault is
```

Edit `.env` with an [OpenRouter](https://openrouter.ai/keys) key, and `config.yaml` with the path to
your vault. Then:

```bash
uv run vault-rag sync            # index the vault (embeds every note; takes a few minutes)
uv run vault-rag stats
```

## Use

```bash
# Find notes
uv run vault-rag retrieve --query "wireguard setup" --mode fast

# Answer a question, with citations — abstains rather than guessing
uv run vault-rag synthesize --query "How did I set up the VPN, and why that way?"

# Vault health
uv run vault-rag lint --format text
```

`vault-rag schema` prints the full machine-readable command and contract schema. All output is
`{"ok": true, "action", "result", "meta"}` on success and `{"ok": false, "action", "error"}` on
failure (exit 1) — **check `ok`, not the exit code.** Every failure is that envelope, including
bad flags and unknown commands — argparse usage text never reaches stdout.

There is also a Streamlit UI:

```bash
uv run streamlit run scripts/streamlit_app.py
```

## Mutating the vault

All write commands go through the official Obsidian CLI rather than touching files directly, so
wikilinks update on move/rename, unknown frontmatter keys survive patches, and vault plugins fire
exactly as if you had edited in the app. **The Obsidian app must be running** for these commands
(macOS only):

```bash
uv run vault-rag create-note   --path "Inbox/New Idea.md" --content-file draft.txt \
                               --frontmatter '{"id":"<ULID>","created":"<now>","updated":"<now>"}'
uv run vault-rag read-note     --path "Inbox/New Idea.md" [--frontmatter-only|--body-only]
uv run vault-rag merge-frontmatter --path "..." --patch '{"type":"idea","aliases":["Alias"]}'
uv run vault-rag add-links     --path "..." --links '[{"target":"Some Note","anchor_text":"some note","line":12}]'
uv run vault-rag insert-related --path "..." --targets '["Some Note"]'
uv run vault-rag move-note     --path "Inbox/New Idea.md" --to "Research/"
uv run vault-rag rename-note   --path "Inbox/New Idea.md" --name "Better Title"
uv run vault-rag open-note     --path "..."
```

Safety properties, enforced in code:

- **Every mutating command takes `--dry-run`**: it computes and returns exactly what would change
  (`changed`, diffs) with `meta.dry_run: true` and makes no backend mutation calls.
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
| `invalid_timestamps` | naive or unparseable `created`/`updated`/`date` |
| `orphans` | notes with no links in or out |
| `stale_distilled` | a distilled note whose sources changed after it was written |

Link resolution follows Obsidian: frontmatter links (`parents: "[[Daily Notes]]"`) are real edges,
`aliases` resolve, and `[[diagram.png]]` resolves to an attachment rather than being called broken.

Two opt-in fixers write to the vault:

```bash
uv run vault-rag lint --fix              # add MISSING id/created/updated (never edits a value)
uv run vault-rag lint --fix-timestamps   # rewrite naive timestamps as offset-aware
```

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
`obsidian.vault`, `obsidian.manage_updated`). Secrets stay in `.env`.

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
