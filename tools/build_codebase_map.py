"""Generate docs/codebase-map.json and docs/codebase-map.html.

Walks the git-tracked Python source with ``ast`` to extract modules, classes,
functions, signatures, and internal imports, then renders two snapshots of the
codebase: a machine-readable JSON map for agents and a self-contained HTML
overview page for humans. Structure comes from the source; the prose (package
roles, data flows, invariants, command table) is maintained by hand below and
should be kept in sync with AGENTS.md when the architecture changes.

Usage:  uv run python tools/build_codebase_map.py
"""

from __future__ import annotations

import ast
import html
import json
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- extraction


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    parts = []
    args = fn.args
    pos = args.posonlyargs + args.args
    defaults = [None] * (len(pos) - len(args.defaults)) + list(args.defaults)
    for a, d in zip(pos, defaults):
        s = a.arg
        if a.annotation is not None:
            s += ": " + ast.unparse(a.annotation)
        if d is not None:
            s += "=" + ast.unparse(d)
        parts.append(s)
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        parts.append("*")
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        s = a.arg
        if a.annotation is not None:
            s += ": " + ast.unparse(a.annotation)
        if d is not None:
            s += "=" + ast.unparse(d)
        parts.append(s)
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    ret = ""
    if fn.returns is not None:
        ret = " -> " + ast.unparse(fn.returns)
    return f"({', '.join(parts)}){ret}"


def _first_line(doc: str | None) -> str | None:
    if not doc:
        return None
    return doc.strip().splitlines()[0].strip()


def extract_modules() -> list[dict]:
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    py_files = [
        f for f in tracked
        if f.endswith(".py") and f.split("/")[0] in
        ("vault_spider", "scripts", "tools", "tests")
    ]

    modules = []
    for rel in sorted(py_files):
        src = (ROOT / rel).read_text()
        tree = ast.parse(src)
        internal_deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
                if node.level > 0:
                    pkg_parts = rel.split("/")[:-1]
                    base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                    mod = ".".join(base + [node.module])
                if mod.startswith("vault_spider"):
                    internal_deps.add(mod)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("vault_spider"):
                        internal_deps.add(alias.name)

        classes, functions, constants = [], [], []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods, fields = [], []
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not item.name.startswith("_") or item.name == "__init__":
                            methods.append({
                                "name": item.name,
                                "signature": _signature(item),
                                "doc": _first_line(ast.get_docstring(item)),
                                "line": item.lineno,
                            })
                    elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        fields.append(item.target.id)
                classes.append({
                    "name": node.name,
                    "bases": [ast.unparse(b) for b in node.bases],
                    "doc": _first_line(ast.get_docstring(node)),
                    "line": node.lineno,
                    "fields": fields,
                    "methods": methods,
                })
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    functions.append({
                        "name": node.name,
                        "signature": _signature(node),
                        "doc": _first_line(ast.get_docstring(node)),
                        "line": node.lineno,
                    })
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) \
                    and node.targets[0].id.isupper():
                constants.append(node.targets[0].id)

        modules.append({
            "path": rel,
            "loc": len(src.splitlines()),
            "doc": ast.get_docstring(tree),
            "internal_deps": sorted(internal_deps),
            "classes": classes,
            "functions": functions,
            "constants": constants,
        })
    return modules


# ------------------------------------------------------ hand-maintained prose

PACKAGES = {
    "vault_spider (root)": {
        "match": lambda p: p.startswith("vault_spider/") and "/" not in p[len("vault_spider/"):],
        "role": "Entry points and cross-cutting plumbing: the JSON-only CLI, the MCP server, "
                "configuration, the JSON envelope, and shared helpers.",
    },
    "vault_spider.corpus": {
        "match": lambda p: p.startswith("vault_spider/corpus/"),
        "role": "Reads the vault: loads Markdown notes, parses frontmatter, resolves note "
                "identity (ULID or path hash), and splits notes into deterministic "
                "heading-based sections.",
    },
    "vault_spider.index": {
        "match": lambda p: p.startswith("vault_spider/index/"),
        "role": "Builds and maintains the ChromaDB collection (one document entry + N section "
                "entries per note, distinguished by the `granularity` metadata field) with "
                "failure-safe incremental sync; plus a read-only reader for the Streamlit UI.",
    },
    "vault_spider.retrieval": {
        "match": lambda p: p.startswith("vault_spider/retrieval/"),
        "role": "Hybrid search: BM25 + embeddings fused by pure scoring functions, optional "
                "rerank (thorough mode), an on-disk query-embedding cache, and assembly of the "
                "retrieval output contract (evidence/citations).",
    },
    "vault_spider.synthesis": {
        "match": lambda p: p.startswith("vault_spider/synthesis/"),
        "role": "Turns a retrieval output contract into an LLM-synthesized, cited answer; "
                "abstains when the notes lack the answer.",
    },
    "vault_spider.compounding": {
        "match": lambda p: p.startswith("vault_spider/compounding/"),
        "role": "Vault health and write-back: the read-only lint report, saving good answers "
                "as distilled notes, and shared frontmatter-backfill mechanics.",
    },
    "vault_spider.enrich": {
        "match": lambda p: p.startswith("vault_spider/enrich/"),
        "role": "App-agnostic enrichment planner: retrieves a note's neighborhood and proposes "
                "title/frontmatter/links/placement as JSON. Never mutates files or the index; "
                "plans are applied via the mutation commands.",
    },
    "vault_spider.obsidian": {
        "match": lambda p: p.startswith("vault_spider/obsidian/"),
        "role": "The write path: contract-enforcing note mutations executed through the "
                "official Obsidian CLI (app must be running, macOS only), plus the macOS vault "
                "registry bridge. Mutation code never writes vault files directly.",
    },
    "vault_spider.evaluation": {
        "match": lambda p: p.startswith("vault_spider/evaluation/"),
        "role": "Golden-dataset benchmark: strict label validation against the corpus, then "
                "scored retrieval (nDCG@k, evidence recall, complete@k, MRR) and optional "
                "LLM-judged synthesis runs. Always run against a dedicated --chroma-path.",
    },
    "vault_spider.llm": {
        "match": lambda p: p.startswith("vault_spider/llm/"),
        "role": "OpenRouter client: embeddings, reranking, and chat completions. The only "
                "module that talks to the LLM provider.",
    },
    "scripts": {
        "match": lambda p: p.startswith("scripts/"),
        "role": "Operational surfaces: the Streamlit UI (app/search/llm/db pages), launchd "
                "periodic-sync installer and runner, and the Obsidian-side setup script.",
    },
    "tools": {
        "match": lambda p: p.startswith("tools/"),
        "role": "Maintenance tools: frontmatter backfills for existing vaults (dry-run by "
                "default) and this codebase-map generator.",
    },
}

DATA_FLOW = {
    "read_path": [
        "vault .md files",
        "corpus.loader / corpus.frontmatter / corpus.identity / corpus.chunker",
        "index.store (Chroma collection: document + section entries; BM25 corpus)",
        "retrieval.searcher (BM25 + embeddings via llm.openrouter, fused by retrieval.fusion; "
        "thorough mode reranks)",
        "retrieval.evidence (retrieval output contract)",
        "synthesis.answer (cited answer or abstention)",
        "compounding.distill (--save: write answer back as a distilled note)",
    ],
    "write_path": [
        "cli.py mutation commands (create-note, edit-note, merge-frontmatter, add-links, "
        "insert-related, move-note, rename-note, open-note)",
        "obsidian.notes (contract enforcement, dry-run diffs, sha256 compare-and-write)",
        "obsidian.backend (official Obsidian CLI invocation)",
        "running Obsidian app -> vault files (wikilinks update, plugins fire)",
    ],
    "surfaces": [
        "bin/vault-spider -> vault_spider.cli (JSON envelope on stdout, exit 1 on error)",
        "bin/vault-spider-mcp -> vault_spider.mcp_server (dual-transport MCP for Claude "
        "Desktop / ChatGPT)",
        "scripts/streamlit_app.py (interactive UI)",
    ],
}

INVARIANTS = [
    "Every CLI command prints exactly one JSON envelope: {ok: true, action, result, meta} or "
    "{ok: false, action, error}; check `ok`, not just the exit code.",
    "Read path (sync, retrieve, lint) works on vault files directly; the write path goes only "
    "through the Obsidian CLI. Never write vault files directly from mutation code.",
    "One note indexes as one document-granularity entry plus N section entries in a single "
    "Chroma collection (granularity metadata field). Section splitting is deterministic and "
    "heading-based.",
    "`provenance` frontmatter (human | reference | llm | distilled) is set at note creation, "
    "immutable once set, never proposed by enrichment, and orthogonal to `type`.",
    "`id`, `created`, and `provenance` are immutable once set; `lint --fix` writes only "
    "missing values, never edits existing ones.",
    "Sync is failure-safe: old index entries are deleted only after all new embeddings are "
    "computed and validated.",
    "Every mutating command accepts --dry-run and returns exactly what would change with "
    "meta.dry_run: true, making no backend calls.",
    "Eval runs go against a dedicated --chroma-path, never the live-vault index.",
    "Distilled notes are regenerable pointers to their sources; raw notes always win on "
    "conflict.",
]

COMMANDS = [
    {"command": "schema",
     "summary": "Print the machine-readable command + contract schema (version 2)."},
    {"command": "sync [--root DIR] [--reset]",
     "summary": "Incremental index sync: add new, re-embed changed/moved, delete removed notes."},
    {"command": "stats", "summary": "Index statistics (no API key needed)."},
    {"command": "retrieve --query Q [--mode fast|thorough] "
                "[--granularity document|section|mixed] [filters]",
     "summary": "Hybrid retrieval returning the scored-candidate contract. Filters: --folder "
                "--tag --type --provenance --since --until."},
    {"command": "synthesize --query Q [--mode thorough] [--retrieval FILE] [--save]",
     "summary": "Retrieve then synthesize a cited answer; abstains when unsupported; --save "
                "persists a distilled note."},
    {"command": "enrich --root DIR (--note PATH | --stdin) [--intent ...]",
     "summary": "Plan-only enrichment: proposed title/frontmatter/links/placement as JSON; "
                "never mutates."},
    {"command": "lint --root DIR [--format json|text] [--fix] [--fix-timestamps]",
     "summary": "Read-only corpus health report; --fix writes only missing id/created/updated."},
    {"command": "eval validate|run --dataset DIR [--stage retrieval|synthesis]",
     "summary": "Golden-dataset validation and scored benchmark with a versioned results "
                "contract."},
    {"command": "create-note / read-note / edit-note / merge-frontmatter / add-links / "
                "insert-related / move-note / rename-note / open-note",
     "summary": "Note mutations through the running Obsidian app; all mutating commands accept "
                "--dry-run."},
]

OTHER_FILES = [
    {"path": "AGENTS.md", "role": "Canonical project instructions (CLAUDE.md delegates here)."},
    {"path": "README.md", "role": "User-facing overview and install/usage guide."},
    {"path": "bin/vault-spider",
     "role": "Stable CLI wrapper; locates the project and delegates to `uv run vault-spider`."},
    {"path": "bin/vault-spider-mcp", "role": "Wrapper launching the MCP server."},
    {"path": "config.yaml.example",
     "role": "Template for installation settings (vault root, folders, tag rules, timestamps "
             "policy). Real config.yaml is gitignored."},
    {"path": ".env.example", "role": "Template for secrets: OpenRouter key and model names."},
    {"path": "pyproject.toml", "role": "Package metadata and dependencies (uv-managed)."},
    {"path": ".github/workflows/ci.yml", "role": "CI workflow."},
    {"path": "docs/launchd.md",
     "role": "Periodic-sync LaunchAgent: interval, logs, status, uninstall."},
    {"path": "docs/obsidian-setup.md", "role": "Obsidian-side setup for the mutation backend."},
    {"path": "skills/vault/SKILL.md",
     "role": "Agent skill for operating the vault (plus references/ for capture, commands, "
             "eval/server)."},
    {"path": "eval/",
     "role": "Committed golden dataset #1: public_vault corpus, golden_queries.jsonl, "
             "dataset.yaml, eval-config.yaml."},
    {"path": "eval-realistic/",
     "role": "Committed golden dataset #2: larger realistic corpus with the same layout."},
    {"path": "tests/fixtures/notes/", "role": "Small fixture vault used by the pytest suite."},
    {"path": "chroma_db/ (gitignored)", "role": "Local Chroma index of the live vault."},
]

# ---------------------------------------------------------------- JSON output


def build_json(code_modules: list[dict], test_modules: list[dict]) -> dict:
    def strip_module(m: dict) -> dict:
        out = {
            "path": m["path"],
            "loc": m["loc"],
            "summary": (m["doc"] or "").strip().split("\n\n")[0].replace("\n", " ") or None,
            "internal_deps": m["internal_deps"],
            "classes": m["classes"],
            "functions": m["functions"],
        }
        if m["constants"]:
            out["constants"] = m["constants"]
        return out

    tests_json = []
    for t in test_modules:
        n_tests = sum(1 for f in t["functions"] if f["name"].startswith("test_"))
        n_tests += sum(
            1 for c in t["classes"] for meth in c["methods"]
            if meth["name"].startswith("test_")
        )
        tests_json.append({
            "path": t["path"],
            "loc": t["loc"],
            "test_count": n_tests,
            "summary": _first_line(t["doc"]),
            "covers": t["internal_deps"],
        })

    packages_json = {}
    for name, spec in PACKAGES.items():
        mods = [strip_module(m) for m in code_modules if spec["match"](m["path"])]
        mods = [m for m in mods if m["loc"] > 1 or m["summary"]]
        packages_json[name] = {"role": spec["role"], "modules": mods}

    return {
        "project": {
            "name": "vault-spider",
            "description": "JSON-only CLI (plus MCP server and Streamlit UI) for an Obsidian "
                           "vault: hybrid retrieval over ChromaDB + BM25, cited answer "
                           "synthesis with abstention, corpus health lint, plan-only "
                           "enrichment, a golden eval harness, and safe note mutations "
                           "executed through the running Obsidian app.",
            "language": "Python (uv-managed)",
            "entry_points": {
                "cli": "bin/vault-spider -> vault_spider/cli.py:main",
                "mcp": "bin/vault-spider-mcp -> vault_spider/mcp_server.py:main",
                "ui": "scripts/streamlit_app.py",
            },
            "canonical_instructions": "AGENTS.md",
            "json_envelope": "{ok: true, action, result, meta} | {ok: false, action, error}; "
                             "always check `ok`, not just the exit code",
        },
        "generated": date.today().isoformat(),
        "generator_note": "Regenerate with `uv run python tools/build_codebase_map.py`. "
                          "Structure extracted from the git-tracked source via Python ast; "
                          "line numbers refer to the commit current on the generation date.",
        "architecture": {"data_flow": DATA_FLOW, "invariants": INVARIANTS},
        "commands": COMMANDS,
        "packages": packages_json,
        "tests": {"runner": "uv run pytest", "files": tests_json},
        "other_files": OTHER_FILES,
    }


# ---------------------------------------------------------------- HTML output


def build_html(codebase_map: dict) -> str:
    e = html.escape
    packages_json = codebase_map["packages"]
    tests_json = codebase_map["tests"]["files"]

    def module_card(m: dict) -> str:
        parts = [f'<div class="card" id="{e(m["path"])}">']
        parts.append(
            f'<div class="card-head"><code class="path">{e(m["path"])}</code>'
            f'<span class="loc">{m["loc"]} loc</span></div>'
        )
        if m["summary"]:
            parts.append(f'<p class="mod-doc">{e(m["summary"])}</p>')
        if m["internal_deps"]:
            chips = "".join(f'<span class="chip">{e(d)}</span>' for d in m["internal_deps"])
            parts.append(f'<div class="deps">imports {chips}</div>')

        inner = []
        for c in m["classes"]:
            bases = f'({e(", ".join(c["bases"]))})' if c["bases"] else ""
            inner.append(f'<li class="cls"><code>class {e(c["name"])}{bases}</code>'
                         f'{" — " + e(c["doc"]) if c["doc"] else ""}')
            meths = [meth for meth in c["methods"] if meth["name"] != "__init__"]
            if meths:
                inner.append("<ul>")
                for meth in meths:
                    inner.append(
                        f'<li><code>.{e(meth["name"])}{e(meth["signature"])}</code>'
                        f'{" — " + e(meth["doc"]) if meth["doc"] else ""}</li>')
                inner.append("</ul>")
            inner.append("</li>")
        for f in m["functions"]:
            inner.append(f'<li><code>{e(f["name"])}{e(f["signature"])}</code>'
                         f'{" — " + e(f["doc"]) if f["doc"] else ""}</li>')
        if inner:
            n = len(m["classes"]) + len(m["functions"])
            parts.append(
                f'<details><summary>{n} public symbol{"s" if n != 1 else ""}</summary>'
                f'<ul class="symbols">{"".join(inner)}</ul></details>')
        parts.append("</div>")
        return "".join(parts)

    sections_html = []
    for name, spec in packages_json.items():
        cards = "".join(module_card(m) for m in spec["modules"])
        if not cards:
            continue
        sections_html.append(
            f'<section><h2><code>{e(name)}</code></h2>'
            f'<p class="pkg-role">{e(spec["role"])}</p>'
            f'<div class="cards">{cards}</div></section>'
        )

    flow_html = ""
    for title, key in [("Read path (query)", "read_path"),
                       ("Write path (mutation)", "write_path"),
                       ("Surfaces", "surfaces")]:
        steps = "".join(f'<li>{e(s)}</li>' for s in DATA_FLOW[key])
        flow_html += f'<div class="flow"><h3>{title}</h3><ol>{steps}</ol></div>'

    inv_html = "".join(f"<li>{e(i)}</li>" for i in INVARIANTS)
    cmd_rows = "".join(
        f'<tr><td><code>{e(c["command"])}</code></td><td>{e(c["summary"])}</td></tr>'
        for c in COMMANDS
    )
    test_rows = "".join(
        f'<tr><td><code>{e(t["path"])}</code></td><td class="num">{t["test_count"]}</td>'
        f'<td>{"".join(f"<span class=chip>{e(d)}</span>" for d in t["covers"]) or "—"}</td></tr>'
        for t in tests_json
    )
    other_rows = "".join(
        f'<tr><td><code>{e(o["path"])}</code></td><td>{e(o["role"])}</td></tr>'
        for o in OTHER_FILES
    )

    total_loc = sum(m["loc"] for p in packages_json.values() for m in p["modules"])
    n_mods = sum(len(p["modules"]) for p in packages_json.values())
    n_tests_total = sum(t["test_count"] for t in tests_json)
    today = codebase_map["generated"]

    return f"""<!doctype html>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>vault-spider — codebase map</title>
<style>
:root {{
  --bg: #faf9f6; --fg: #1f2430; --muted: #6b7280; --card: #ffffff;
  --line: #e4e2db; --accent: #4a5f8a; --chip-bg: #eef1f6; --code-bg: #f1efe9;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #16181d; --fg: #e6e4de; --muted: #9aa0ab; --card: #1e2128;
    --line: #2c303a; --accent: #8fa7d4; --chip-bg: #262b36; --code-bg: #23262e; }}
}}
body {{ background: var(--bg); color: var(--fg); margin: 0;
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
main {{ max-width: 62rem; margin: 0 auto; padding: 2.5rem 1.25rem 5rem; }}
h1 {{ font-size: 1.7rem; margin: 0 0 .25rem; }}
h2 {{ font-size: 1.15rem; margin: 2.5rem 0 .25rem; border-bottom: 1px solid var(--line);
  padding-bottom: .35rem; }}
h3 {{ font-size: .95rem; margin: 0 0 .4rem; }}
code {{ font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .86em;
  background: var(--code-bg); padding: .08em .35em; border-radius: 4px; }}
.subtitle {{ color: var(--muted); margin: 0 0 1rem; }}
.stats {{ display: flex; gap: 1.75rem; flex-wrap: wrap; margin: 1.25rem 0 0; }}
.stat b {{ display: block; font-size: 1.3rem; }}
.stat span {{ color: var(--muted); font-size: .82rem; }}
.flows {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(17rem, 1fr));
  gap: 1rem; margin-top: 1rem; }}
.flow {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 1rem 1.1rem; }}
.flow ol {{ margin: 0; padding-left: 1.2rem; }}
.flow li {{ margin: .3rem 0; font-size: .88rem; }}
.flow li::marker {{ color: var(--accent); }}
.pkg-role {{ color: var(--muted); margin: .3rem 0 .9rem; font-size: .92rem; }}
.cards {{ display: grid; gap: .8rem; }}
.card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: .85rem 1rem; }}
.card-head {{ display: flex; justify-content: space-between; gap: 1rem;
  align-items: baseline; }}
.path {{ font-weight: 600; color: var(--accent); background: none; padding: 0; }}
.loc {{ color: var(--muted); font-size: .78rem; white-space: nowrap; }}
.mod-doc {{ margin: .35rem 0 .2rem; font-size: .9rem; }}
.deps {{ margin-top: .35rem; font-size: .78rem; color: var(--muted); }}
.chip {{ display: inline-block; background: var(--chip-bg); border-radius: 999px;
  padding: .05em .6em; margin: .1em .15em; font-family: ui-monospace, Menlo, monospace;
  font-size: .74rem; }}
details {{ margin-top: .5rem; }}
summary {{ cursor: pointer; color: var(--accent); font-size: .85rem; }}
.symbols {{ margin: .5rem 0 0; padding-left: 1.1rem; font-size: .85rem; }}
.symbols li {{ margin: .25rem 0; }}
.symbols ul {{ padding-left: 1.2rem; }}
.cls > code {{ font-weight: 600; }}
table {{ border-collapse: collapse; width: 100%; font-size: .88rem; margin-top: .75rem; }}
th, td {{ text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--line);
  vertical-align: top; }}
th {{ color: var(--muted); font-weight: 600; font-size: .78rem; text-transform: uppercase;
  letter-spacing: .04em; }}
.num {{ text-align: right; }}
.tablewrap {{ overflow-x: auto; }}
ul.invariants li {{ margin: .4rem 0; font-size: .92rem; }}
footer {{ margin-top: 3rem; color: var(--muted); font-size: .8rem; }}
</style>
<main>
<h1>vault-spider — codebase map</h1>
<p class="subtitle">JSON-only CLI, MCP server, and Streamlit UI for an Obsidian vault:
hybrid retrieval (ChromaDB + BM25), cited answer synthesis with abstention, corpus lint,
plan-only enrichment, a golden eval harness, and safe note mutations through the running
Obsidian app. Canonical instructions: <code>AGENTS.md</code>.</p>
<div class="stats">
  <div class="stat"><b>{n_mods}</b><span>source modules</span></div>
  <div class="stat"><b>{total_loc:,}</b><span>lines of Python (src)</span></div>
  <div class="stat"><b>{len(tests_json)}</b><span>test files</span></div>
  <div class="stat"><b>{n_tests_total}</b><span>test functions</span></div>
</div>

<h2>Architecture</h2>
<div class="flows">{flow_html}</div>

<h2>Invariants</h2>
<ul class="invariants">{inv_html}</ul>

<h2>CLI commands</h2>
<div class="tablewrap"><table>
<tr><th>Command</th><th>What it does</th></tr>
{cmd_rows}
</table></div>

{"".join(sections_html)}

<h2>Tests</h2>
<p class="pkg-role">Run with <code>uv run pytest</code>. "Covers" is derived from each test
file's <code>vault_spider</code> imports.</p>
<div class="tablewrap"><table>
<tr><th>File</th><th class="num">Tests</th><th>Covers</th></tr>
{test_rows}
</table></div>

<h2>Other important files</h2>
<div class="tablewrap"><table>
<tr><th>Path</th><th>Role</th></tr>
{other_rows}
</table></div>

<footer>Generated {today} by <code>tools/build_codebase_map.py</code> from the git-tracked
source via Python <code>ast</code>. Companion machine-readable file:
<code>docs/codebase-map.json</code>.</footer>
</main>
"""


def main() -> None:
    modules = extract_modules()
    code_modules = [m for m in modules if not m["path"].startswith("tests/")]
    test_modules = [m for m in modules if m["path"].startswith("tests/")]

    codebase_map = build_json(code_modules, test_modules)

    json_path = ROOT / "docs" / "codebase-map.json"
    json_path.write_text(json.dumps(codebase_map, indent=2) + "\n")
    print(f"wrote {json_path.relative_to(ROOT)} ({json_path.stat().st_size} bytes)")

    html_path = ROOT / "docs" / "codebase-map.html"
    html_path.write_text(build_html(codebase_map))
    print(f"wrote {html_path.relative_to(ROOT)} ({html_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
