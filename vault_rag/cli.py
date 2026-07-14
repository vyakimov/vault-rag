"""JSON-only CLI for the vault: retrieval/synthesis plus safe note mutations."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import PurePosixPath
from typing import Any, Dict, Optional

from vault_rag import settings
from vault_rag.envelope import CliError, failure, print_json, success
from vault_rag.llm.openrouter import OpenRouterClient, OpenRouterError

# v2: the obsctl note-mutation commands were merged into this CLI (one schema,
# one envelope, one error-type union).
SCHEMA_VERSION = 2

# How many findings per check `--format text` prints before truncating.
_LINT_TEXT_LIMIT = 15


def get_provider() -> OpenRouterClient:
    try:
        return OpenRouterClient.from_env()
    except ValueError as exc:
        # Missing OPENROUTER_API_KEY should surface as provider_error, not
        # internal_error.
        raise OpenRouterError(str(exc)) from exc


def get_store(chroma_path: str, collection: str, provider: Optional[OpenRouterClient] = None):
    # Imported lazily so `vault-rag schema` works without chromadb/model setup.
    from vault_rag.index.store import IndexStore

    return IndexStore(
        chroma_db_path=chroma_path,
        collection_name=collection,
        provider=provider,
    )


# -- schema -------------------------------------------------------------------

def _schema() -> Dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "commands": {
            "schema": {"args": {}, "result": "this document", "mutates_state": False},
            # mutates_state is always a boolean ("can this command write?");
            # the optional `mutates` string says what and when.
            "sync": {
                "mutates_state": True,
                "mutates": "the index, never the vault",
                "args": {
                    "--root": "vault directory (required unless config.yaml sets vault.root)",
                    "--reset": "flag",
                    "--dry-run": "flag",
                },
                "result": {
                    "added_notes": "int",
                    "updated_notes": "int",
                    "deleted_notes": "int",
                    "unchanged": "int",
                    "total_entries": "int",
                    "warnings": ["str"],
                    "dry_run": "bool",
                    "would_add/would_update/would_delete": ["str (dry-run only)"],
                },
            },
            "stats": {
                "mutates_state": False,
                "args": {},
                "result": {
                    "total_documents": "int",
                    "total_entries": "int",
                    "section_entries": "int",
                    "unique_folders": "int",
                    "unique_tags": "int",
                    "dated_notes": "int",
                    "embedding_model": "str",
                },
            },
            "retrieve": {
                "mutates_state": False,
                "args": {
                    "--query": "str (required)",
                    "--mode": "fast|thorough (default fast)",
                    "--granularity": "document|section|mixed (mixed = section pool, max 3 sections per note; documents are not searched)",
                    "-n": "int (default 10)",
                    "--folder/--tag/--type/--since/--until/--must-include": "metadata and required-term filters",
                },
                "result": "retrieval_output",
            },
            "synthesize": {
                "mutates_state": True,
                "mutates": "the vault, only with --save (writes one new distilled note)",
                "args": {
                    "--query": "str",
                    "--mode": "fast|thorough (default thorough)",
                    "--granularity": "document|section|mixed (mixed = section pool, max 3 sections per note; documents are not searched)",
                    "--retrieval": "path to a prior retrieve envelope/contract",
                    "--n-context": "int (default 8)",
                    "--save": "flag: persist a good answer as a distilled note (needs --root, live query)",
                    "--save-dir": "distilled folder relative to --root (default Distilled)",
                    "--root": "vault directory to write the distilled note into",
                    "--folder/--tag/--type/--since/--until/--must-include": "metadata and required-term filters",
                },
                "result": "synthesis_output (with embedded retrieval; +saved/saved_path when --save)",
            },
            "lint": {
                "mutates_state": True,
                "mutates": "the vault, only with --fix/--fix-timestamps",
                "args": {
                    "--root": "vault directory (required unless config.yaml sets vault.root)",
                    "--format": "json|text (default json)",
                    "--fix": "flag: write missing id/created/updated frontmatter",
                    "--fix-timestamps": "flag: rewrite naive created/updated/date as offset-aware",
                },
                "result": "lint_report (+fixed/fix_skipped with --fix/--fix-timestamps)",
            },
            "enrich": {
                "mutates_state": False,
                "args": {
                    "--root": "corpus directory (required unless config.yaml sets vault.root)",
                    "--note": "vault-relative path (xor --stdin)",
                    "--stdin": "flag: enrich raw text from stdin",
                    "--intent": "free text",
                    "--source-type": "transcript|web|pdf|manual",
                    "--source-url": "url",
                    "--title": "known title override",
                },
                "result": "enrichment_plan",
            },
            # Note mutations. All go through the official Obsidian CLI, so the
            # Obsidian app must be running; every mutating one takes --dry-run
            # (compute + return the diff, mutate nothing) and the per-command
            # connection overrides --binary / --vault.
            "create-note": {
                "mutates_state": True,
                "requires": "obsidian-app",
                "args": {
                    "--path": "vault-relative .md path (required)",
                    "--content": "note body ('-' = stdin)",
                    "--content-file": "read body from file ('-' = stdin; xor --content)",
                    "--frontmatter": "JSON object (only place id/created may be set)",
                    "--dry-run": "flag",
                },
                "result": {"changed": "bool", "path": "str", "text": "str (dry-run only)"},
            },
            "read-note": {
                "mutates_state": False,
                "requires": "obsidian-app",
                "args": {
                    "--path": "vault-relative path (required)",
                    "--frontmatter-only": "flag",
                    "--body-only": "flag",
                },
                "result": {"path": "str", "frontmatter": "object", "body": "str", "raw": "str"},
            },
            "merge-frontmatter": {
                "mutates_state": True,
                "requires": "obsidian-app",
                "args": {
                    "--path": "vault-relative path (required)",
                    "--patch": "JSON object; id/created immutable once set; aliases merge",
                    "--dry-run": "flag",
                },
                "result": {
                    "changed": "bool",
                    "fields_touched": ["str"],
                    "skipped": "object",
                    "diffs": "object (dry-run only)",
                },
            },
            "add-links": {
                "mutates_state": True,
                "requires": "obsidian-app",
                "args": {
                    "--path": "vault-relative path (required)",
                    "--links": 'JSON array of {"target", "anchor_text", "line"?}',
                    "--dry-run": "flag",
                },
                "result": {"changed": "bool", "path": "str", "links": ["per-link outcome"]},
            },
            "insert-related": {
                "mutates_state": True,
                "requires": "obsidian-app",
                "args": {
                    "--path": "vault-relative path (required)",
                    "--targets": "JSON array of wikilink targets",
                    "--dry-run": "flag",
                },
                "result": {"changed": "bool", "added": ["str"], "already_present": ["str"]},
            },
            "move-note": {
                "mutates_state": True,
                "requires": "obsidian-app",
                "args": {
                    "--path": "vault-relative path (required)",
                    "--to": "destination folder (must exist)",
                    "--dry-run": "flag",
                },
                "result": {"changed": "bool", "path_before": "str", "path_after": "str"},
            },
            "rename-note": {
                "mutates_state": True,
                "requires": "obsidian-app",
                "args": {
                    "--path": "vault-relative path (required)",
                    "--name": "new note name (.md optional)",
                    "--dry-run": "flag",
                },
                "result": {"changed": "bool", "path_before": "str", "path_after": "str"},
            },
            "open-note": {
                "mutates_state": False,
                "requires": "obsidian-app",
                "args": {"--path": "vault-relative path (required)"},
                "result": {"opened": "bool", "path": "str"},
            },
        },
        "mutation_contract": {
            "immutable_fields": ["id", "created"],
            "timestamps": "written untyped; format follows config.yaml `timestamps.policy`",
            "manage_updated": "if true (config.yaml `obsidian.manage_updated`), "
                              "content edits patch `updated` themselves",
            "empty_optional_fields": "patches writing '' / [] / null are refused",
            "links_updated_by": "move/rename: the backend rewrites incoming wikilinks",
        },
        "contracts": {
            "retrieval_output": {
                "query": "str",
                "mode": "str",
                "granularity": "str",
                "candidates": [
                    {
                        "note_id": "str",
                        "path": "str",
                        "title": "str",
                        "type": "str",
                        "heading": "str",
                        "chunk_id": "str",
                        "line_start": "int",
                        "line_end": "int",
                        "excerpt": "str",
                        "scores": {
                            "bm25": "float",
                            "semantic": "float",
                            "fused": "float",
                            "reranker": "float|null",
                            "final": "float",
                        },
                        "why": "str",
                    }
                ],
            },
            "synthesis_output": {
                "question": "str",
                "answer": "str",
                "confidence": "str",
                "abstained": "bool",
                "citations": [
                    {
                        "key": "str",
                        "note_id": "str",
                        "path": "str",
                        "title": "str",
                        "heading": "str",
                        "excerpt": "str",
                    }
                ],
                "notes_used": ["str"],
                "warnings": ["str"],
                "retrieval": "retrieval_output",
                "saved": "bool (with --save)",
                "saved_path": "str|null (with --save)",
            },
            "lint_report": {
                "root": "str",
                "notes_scanned": "int",
                "notes_ignored": "int",
                "summary": {
                    "missing_frontmatter_fields": "int",
                    "invalid_timestamps": "int",
                    "duplicate_ids": "int",
                    "duplicate_titles": "int",
                    "broken_wikilinks": "int",
                    "dangling_targets": "int (unresolved targets, aggregated by link count)",
                    "empty_notes": "int (stubs; sorted by inbound links desc)",
                    "conflict_copies": "int ('Note 1.md' beside 'Note.md')",
                    "orphans": "int",
                    "stale_distilled": "int",
                },
                "findings": "object (per-check lists)",
            },
            "enrichment_plan": {
                "input": {"path": "str|null", "given_title": "str|null", "intent": "str|null", "source_type": "str|null"},
                "title": "str",
                "title_changed": "bool",
                "suggested_path": "str",
                "frontmatter_patch": "object (type/aliases/source_type/source_url only)",
                "link_insertions": [
                    {"target": "str", "target_path": "str", "confidence": "float", "mode": "inline", "anchor_text": "str", "occurs_at_line": "int"}
                ],
                "related_candidates": [
                    {"target": "str", "target_path": "str", "confidence": "float", "reason": "str"}
                ],
                "warnings": ["str"],
                "confidence": "high|medium|low",
            },
        },
        "error_types": [
            "invalid_arguments",
            "index_empty",
            "provider_error",
            "not_found",
            "internal_error",
            "obsidian_not_running",
            "backend_error",
            "already_exists",
            "ambiguous_target",
            "contract_violation",
        ],
    }


# -- command handlers ---------------------------------------------------------

def cmd_schema(args: argparse.Namespace) -> Dict[str, Any]:
    return success("schema", result=_schema(), meta={"version": SCHEMA_VERSION})


def cmd_sync(args: argparse.Namespace) -> Dict[str, Any]:
    root = args.root
    if not os.path.isdir(root):
        return failure("sync", "invalid_arguments", f"root directory not found: {root}")
    if args.reset and args.dry_run:
        return failure(
            "sync", "invalid_arguments", "--reset cannot be combined with --dry-run"
        )
    provider = get_provider()
    store = get_store(args.chroma_path, args.collection, provider)
    result = store.sync(root, reset=args.reset, dry_run=args.dry_run)
    return success(
        "sync",
        result=result,
        meta={"root": root, "reset": args.reset, "dry_run": args.dry_run},
    )


def cmd_stats(args: argparse.Namespace) -> Dict[str, Any]:
    from vault_rag.index.reader import DatabaseReader

    reader = DatabaseReader(args.chroma_path, args.collection)
    if reader.collection is None or reader.collection.count() == 0:
        return failure(
            "stats",
            "index_empty",
            "index is empty; run `vault-rag sync --root <dir>` first",
        )
    return success("stats", result=reader.get_collection_stats())


def _run_retrieval(store, provider, query, mode, granularity, n_results, args):
    from vault_rag.retrieval.evidence import build_retrieval_output
    from vault_rag.retrieval.searcher import Searcher

    searcher = Searcher(store, granularity=granularity, provider=provider)
    result = searcher.hybrid_search(
        query,
        mode=mode,
        granularity=granularity,
        n_results=n_results,
        folder=args.folder,
        tags=args.tags,
        note_type=args.note_type,
        since=args.since,
        until=args.until,
        must_include_terms=args.must_include_terms,
    )
    output = build_retrieval_output(query, mode, granularity, result.rows)
    return output, result


def _validate_filter_dates(args: argparse.Namespace) -> Optional[str]:
    from datetime import datetime

    for name in ("since", "until"):
        value = getattr(args, name, None)
        if value is None:
            continue
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return f"invalid --{name} date: {value}"
    return None


def cmd_retrieve(args: argparse.Namespace) -> Dict[str, Any]:
    if not args.query or not args.query.strip():
        return failure("retrieve", "invalid_arguments", "--query is required")
    date_error = _validate_filter_dates(args)
    if date_error:
        return failure("retrieve", "invalid_arguments", date_error)
    provider = get_provider()
    store = get_store(args.chroma_path, args.collection, provider)
    if store.collection.count() == 0:
        return failure(
            "retrieve",
            "index_empty",
            "index is empty; run `vault-rag sync --root <dir>` first",
        )
    try:
        output, result = _run_retrieval(
            store, provider, args.query, args.mode, args.granularity, args.n, args
        )
    except OpenRouterError as exc:
        return failure("retrieve", "provider_error", str(exc))
    except ValueError as exc:
        return failure("retrieve", "not_found", str(exc))
    meta = {"timing_ms": round(result.timing_ms, 2), "tunables": result.debug_info}
    return success("retrieve", result=output, meta=meta)


def _load_retrieval_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict) and "candidates" in payload:
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        return payload["result"]
    raise ValueError("retrieval file is not a valid retrieve contract or envelope")


def cmd_synthesize(args: argparse.Namespace) -> Dict[str, Any]:
    date_error = _validate_filter_dates(args)
    if date_error:
        return failure("synthesize", "invalid_arguments", date_error)
    provider = get_provider()

    if args.save and args.retrieval:
        return failure(
            "synthesize",
            "invalid_arguments",
            "--save cannot be combined with --retrieval (replay); it needs a live query",
        )
    if args.save and not args.root:
        return failure("synthesize", "invalid_arguments", "--root is required with --save")

    if args.retrieval:
        try:
            retrieval_output = _load_retrieval_file(args.retrieval)
        except FileNotFoundError:
            return failure("synthesize", "not_found", f"retrieval file not found: {args.retrieval}")
        except (ValueError, json.JSONDecodeError) as exc:
            return failure("synthesize", "invalid_arguments", str(exc))
        query = args.query or str(retrieval_output.get("query", ""))
        if not query.strip():
            return failure(
                "synthesize",
                "invalid_arguments",
                "--query is required: the retrieval file has no query",
            )
    else:
        query = args.query
        if not query or not query.strip():
            return failure(
                "synthesize",
                "invalid_arguments",
                "--query is required unless --retrieval is provided",
            )
        store = get_store(args.chroma_path, args.collection, provider)
        if store.collection.count() == 0:
            return failure(
                "synthesize",
                "index_empty",
                "index is empty; run `vault-rag sync --root <dir>` first",
            )
        try:
            retrieval_output, _ = _run_retrieval(
                store, provider, query, args.mode, args.granularity, args.n, args
            )
        except OpenRouterError as exc:
            return failure("synthesize", "provider_error", str(exc))
        except ValueError as exc:
            return failure("synthesize", "not_found", str(exc))

    from vault_rag.synthesis.answer import synthesize as synthesize_answer

    try:
        synth = synthesize_answer(
            provider, retrieval_output, question=query, hard_cutoff=args.n_context
        )
    except OpenRouterError as exc:
        return failure("synthesize", "provider_error", str(exc))

    meta: Dict[str, Any] = {}
    if args.save:
        from vault_rag.compounding.distill import EmptySlugError, save_distilled_note

        try:
            save_result = save_distilled_note(synth, args.root, args.save_dir)
        except EmptySlugError as exc:
            return failure("synthesize", "invalid_arguments", str(exc))
        synth["saved"] = save_result["saved"]
        synth["saved_path"] = save_result["saved_path"]
        synth.setdefault("warnings", []).extend(save_result["warnings"])
        if save_result["saved"]:
            meta["hint"] = "run vault-rag sync to index the distilled note"

    synth["retrieval"] = retrieval_output
    return success("synthesize", result=synth, meta=meta)


def _derive_title(body: str, fallback: str) -> str:
    for line in body.split("\n"):
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if heading and heading.group(1).strip():
            return heading.group(1).strip()
    for line in body.split("\n"):
        if line.strip():
            return line.strip()[:100]
    return fallback


def cmd_enrich(args: argparse.Namespace) -> Dict[str, Any]:
    if bool(args.note) == bool(args.stdin):
        return failure(
            "enrich", "invalid_arguments", "provide exactly one of --note or --stdin"
        )

    from vault_rag.corpus.frontmatter import split_frontmatter
    from vault_rag.enrich.planner import EnrichInput, plan

    if args.note:
        note_path = os.path.join(args.root, args.note)
        if not os.path.isfile(note_path):
            return failure("enrich", "not_found", f"note not found: {args.note}")
        with open(note_path, "r", encoding="utf-8", errors="strict") as handle:
            raw = handle.read()
        frontmatter, body = split_frontmatter(raw)
        title = args.title or str(frontmatter.get("title") or "") or _derive_title(
            body, PurePosixPath(args.note).stem
        )
        rel_path: Optional[str] = args.note
    else:
        body = sys.stdin.read()
        frontmatter = {}
        title = args.title or _derive_title(body, "Untitled")
        rel_path = None

    provider = get_provider()
    store = get_store(args.chroma_path, args.collection, provider)
    if store.collection.count() == 0:
        return failure(
            "enrich", "index_empty", "index is empty; run `vault-rag sync --root <dir>` first"
        )

    inp = EnrichInput(
        body=body,
        title=title,
        path=rel_path,
        existing_frontmatter=frontmatter,
        given_title=args.title,
        intent=args.intent,
        source_type=args.source_type,
        source_url=args.source_url,
    )
    try:
        result = plan(inp, store, provider)
    except OpenRouterError as exc:
        return failure("enrich", "provider_error", str(exc))
    return success("enrich", result=result)


def _lint_text(report: Dict[str, Any]) -> str:
    findings = report["findings"]

    def fmt(check: str, entry: Dict[str, Any]) -> str:
        """One human line per finding — never a raw dict."""
        if check == "broken_wikilinks":
            where = f"{entry['path']}:{entry['line']}"
            note = "" if entry.get("location") == "body" else " (frontmatter)"
            return f"[[{entry['target']}]] <- {where}{note}"
        if check == "dangling_targets":
            linked = ", ".join(entry["linked_from"][:3])
            more = f" (+{len(entry['linked_from']) - 3} more)" if len(entry["linked_from"]) > 3 else ""
            return f"{entry['count']}x  [[{entry['target']}]] <- {linked}{more}"
        if check == "empty_notes":
            return f"{entry['inbound']} inbound, {entry['chars']} chars  {entry['path']}"
        if check == "conflict_copies":
            return f"{entry['path']}  (similarity {entry['similarity']} to {entry['base_path']})"
        if check == "invalid_timestamps":
            return f"{entry['path']}  {entry['field']}: {entry['value']!r} ({entry['problem']})"
        if check == "missing_frontmatter_fields":
            return f"{entry['path']}  missing: {', '.join(entry['missing'])}"
        if check in ("duplicate_ids", "duplicate_titles"):
            key = entry.get("id") or entry.get("title")
            return f"{key}  -> {', '.join(entry['paths'])}"
        if check == "orphans":
            return str(entry["path"])
        if check == "stale_distilled":
            return f"{entry['path']}  {entry.get('warning', 'sources changed since it was written')}"
        return str(entry)

    lines = [
        f"Vault lint: {report['root']}",
        f"  {report['notes_scanned']} notes scanned, {report['notes_ignored']} ignored",
        "",
    ]

    clean = [check for check, count in report["summary"].items() if not count]
    problems = {check: count for check, count in report["summary"].items() if count}

    if not problems:
        lines.append("No findings. The vault is clean.")
        return "\n".join(lines)

    lines.append("Summary")
    for check, count in sorted(problems.items(), key=lambda item: -item[1]):
        lines.append(f"  {count:>5}  {check.replace('_', ' ')}")
    if clean:
        lines.append(f"  clean: {', '.join(check.replace('_', ' ') for check in sorted(clean))}")

    # `dangling_targets` already aggregates `broken_wikilinks`; showing both is noise.
    shown = [check for check in problems if check != "broken_wikilinks"]
    for check in shown:
        entries = findings[check]
        lines.append("")
        header = check.replace("_", " ")
        if len(entries) > _LINT_TEXT_LIMIT:
            header += f" (showing {_LINT_TEXT_LIMIT} of {len(entries)})"
        lines.append(f"{header}:")
        for entry in entries[:_LINT_TEXT_LIMIT]:
            lines.append(f"  {fmt(check, entry)}")

    return "\n".join(lines)


def cmd_lint(args: argparse.Namespace) -> Dict[str, Any]:
    if not os.path.isdir(args.root):
        return failure("lint", "invalid_arguments", f"root directory not found: {args.root}")

    from vault_rag.compounding.lint import (
        fix_missing_frontmatter,
        fix_naive_timestamps,
        lint_vault,
    )

    # Fixes run first; the report always reflects the vault's final state.
    if args.fix or args.fix_timestamps:
        fixed: list = []
        fix_skipped: list = []
        if args.fix:
            added, skipped = fix_missing_frontmatter(args.root)
            fixed.extend(added)
            fix_skipped.extend(skipped)
        if args.fix_timestamps:
            normalized, skipped = fix_naive_timestamps(args.root)
            fixed.extend(normalized)
            fix_skipped.extend(skipped)
        report = lint_vault(args.root)
        report["fixed"] = fixed
        report["fix_skipped"] = fix_skipped
    else:
        report = lint_vault(args.root)
    if args.format == "text":
        text = _lint_text(report)
        if args.fix or args.fix_timestamps:
            text += f"\nfixed: {len(report['fixed'])}"
        sys.stdout.write(text + "\n")
        return {"ok": True, "_no_print": True}
    return success("lint", result=report)


def _obsidian_handler(args: argparse.Namespace) -> Dict[str, Any]:
    """Route a note-mutation subcommand to vault_rag.obsidian with the
    configured connection facts (CLI flags override config.yaml).

    Imported lazily so the query commands never load the mutation stack."""
    from vault_rag.obsidian import backend, notes

    backend.configure(
        binary=args.binary or settings.obsidian_binary(),
        vault=args.vault or settings.obsidian_vault(),
        manage_updated=settings.obsidian_manage_updated(),
    )
    t0 = time.monotonic()
    envelope = notes.HANDLERS[args.command](args)
    meta = envelope.setdefault("meta", {})
    meta["backend"] = "obsidian-cli"
    meta["timing_ms"] = round((time.monotonic() - t0) * 1000)
    return envelope


# -- parser -------------------------------------------------------------------

def _add_filter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--folder", default=None)
    parser.add_argument("--tag", dest="tags", action="append", default=None)
    parser.add_argument("--type", dest="note_type", default=None)
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument(
        "--must-include", dest="must_include_terms", action="append", default=None
    )


def build_parser() -> argparse.ArgumentParser:
    # A configured `vault.root` makes --root optional; without one it stays required.
    _VAULT_ROOT = settings.vault_root()
    parser = argparse.ArgumentParser(prog="vault-rag", description="Vault RAG JSON CLI")
    parser.add_argument(
        "--chroma-path",
        default=settings.chroma_path(),
        help="Chroma persistence dir (default from config.yaml `index.chroma_path`)",
    )
    parser.add_argument("--collection", default="vault_notes", help="Chroma collection name")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--chroma-path", default=argparse.SUPPRESS, help="Chroma persistence dir"
    )
    common.add_argument(
        "--collection", default=argparse.SUPPRESS, help="Chroma collection name"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "schema", parents=[common], help="Print machine-readable command + contract schema"
    )

    p_sync = sub.add_parser(
        "sync", parents=[common], help="Incrementally sync the vault into the index"
    )
    p_sync.add_argument(
        "--root",
        required=_VAULT_ROOT is None,
        default=_VAULT_ROOT,
        help="Vault directory to index (default from config.yaml `vault.root`)",
    )
    p_sync.add_argument("--reset", action="store_true", help="Rebuild from scratch")
    p_sync.add_argument("--dry-run", dest="dry_run", action="store_true")

    sub.add_parser("stats", parents=[common], help="Index statistics")

    p_retrieve = sub.add_parser("retrieve", parents=[common], help="Retrieve candidate notes")
    p_retrieve.add_argument("--query", required=True)
    p_retrieve.add_argument("--mode", choices=["fast", "thorough"], default="fast")
    p_retrieve.add_argument(
        "--granularity", choices=["document", "section", "mixed"], default="document"
    )
    p_retrieve.add_argument("-n", type=int, default=10)
    _add_filter_arguments(p_retrieve)

    p_synth = sub.add_parser(
        "synthesize", parents=[common], help="Retrieve then synthesize an answer"
    )
    p_synth.add_argument("--query", default=None)
    p_synth.add_argument("--mode", choices=["fast", "thorough"], default="thorough")
    p_synth.add_argument(
        "--granularity", choices=["document", "section", "mixed"], default="mixed"
    )
    p_synth.add_argument("--retrieval", default=None, help="Prior retrieve envelope/contract")
    p_synth.add_argument("-n", type=int, default=10)
    p_synth.add_argument("--n-context", dest="n_context", type=int, default=8)
    p_synth.add_argument("--save", action="store_true", help="Persist a good answer as a distilled note")
    p_synth.add_argument(
        "--save-dir",
        dest="save_dir",
        default=settings.distilled_dir(),
        help="Distilled note folder relative to --root (config: `vault.distilled_dir`)",
    )
    p_synth.add_argument(
        "--root",
        default=_VAULT_ROOT,
        help="Vault directory to write the distilled note into (config: `vault.root`)",
    )
    _add_filter_arguments(p_synth)

    p_lint = sub.add_parser("lint", parents=[common], help="Read-only corpus health report")
    p_lint.add_argument(
        "--root",
        required=_VAULT_ROOT is None,
        default=_VAULT_ROOT,
        help="Vault directory to lint (default from config.yaml `vault.root`)",
    )
    p_lint.add_argument("--format", choices=["json", "text"], default="json")
    p_lint.add_argument(
        "--fix", action="store_true", help="Write missing id/created/updated frontmatter"
    )
    p_lint.add_argument(
        "--fix-timestamps",
        action="store_true",
        help="Rewrite naive created/updated/date as offset-aware timestamps",
    )

    p_enrich = sub.add_parser(
        "enrich", parents=[common], help="Propose an enrichment plan (no mutations)"
    )
    p_enrich.add_argument(
        "--root",
        required=_VAULT_ROOT is None,
        default=_VAULT_ROOT,
        help="Corpus directory (default from config.yaml `vault.root`)",
    )
    p_enrich.add_argument("--note", default=None, help="Vault-relative path of an existing note")
    p_enrich.add_argument("--stdin", action="store_true", help="Enrich raw text read from stdin")
    p_enrich.add_argument("--intent", default=None)
    p_enrich.add_argument(
        "--source-type", dest="source_type",
        choices=["transcript", "web", "pdf", "manual"], default=None,
    )
    p_enrich.add_argument("--source-url", dest="source_url", default=None)
    p_enrich.add_argument("--title", default=None, help="Known title override")

    # Note mutations, executed through the running Obsidian app. All take
    # --path plus the connection overrides; the mutating ones add --dry-run.
    obsidian_common = argparse.ArgumentParser(add_help=False)
    obsidian_common.add_argument("--path", required=True, help="Vault-relative note path")
    obsidian_common.add_argument(
        "--binary", default=None, help="Obsidian CLI binary (config: `obsidian.binary`)"
    )
    obsidian_common.add_argument(
        "--vault", default=None, help="Obsidian vault name (config: `obsidian.vault`)"
    )
    mutating = argparse.ArgumentParser(add_help=False, parents=[obsidian_common])
    mutating.add_argument("--dry-run", action="store_true", dest="dry_run")

    p_create = sub.add_parser(
        "create-note", parents=[mutating], help="Create a note (fails if it exists)"
    )
    p_create.add_argument("--content", default=None)
    p_create.add_argument("--content-file", dest="content_file", default=None)
    p_create.add_argument("--frontmatter", default=None)

    p_read = sub.add_parser(
        "read-note", parents=[obsidian_common], help="Read a note via the Obsidian backend"
    )
    p_read.add_argument("--frontmatter-only", action="store_true", dest="frontmatter_only")
    p_read.add_argument("--body-only", action="store_true", dest="body_only")

    p_merge = sub.add_parser(
        "merge-frontmatter", parents=[mutating], help="Merge a frontmatter patch"
    )
    p_merge.add_argument("--patch", required=True)

    p_links = sub.add_parser(
        "add-links", parents=[mutating], help="Turn anchor text into wikilinks"
    )
    p_links.add_argument("--links", required=True)

    p_related = sub.add_parser(
        "insert-related", parents=[mutating], help="Add targets to '## Related'"
    )
    p_related.add_argument("--targets", required=True)

    p_move = sub.add_parser(
        "move-note", parents=[mutating], help="Move a note (backend updates links)"
    )
    p_move.add_argument("--to", required=True)

    p_rename = sub.add_parser(
        "rename-note", parents=[mutating], help="Rename a note (backend updates links)"
    )
    p_rename.add_argument("--name", required=True)

    sub.add_parser("open-note", parents=[obsidian_common], help="Open a note in the Obsidian app")

    return parser


# The note-mutation subcommands are not listed here: anything the parser
# accepts that has no query handler dispatches to _obsidian_handler, which
# looks the action up in notes.HANDLERS (the single list of those commands).
_HANDLERS = {
    "schema": cmd_schema,
    "sync": cmd_sync,
    "stats": cmd_stats,
    "retrieve": cmd_retrieve,
    "synthesize": cmd_synthesize,
    "lint": cmd_lint,
    "enrich": cmd_enrich,
}


def main(argv: Optional[list] = None) -> int:
    try:
        parser = build_parser()
    except settings.ConfigError as exc:
        # A malformed config.yaml must still leave one JSON envelope on stdout.
        command = next((arg for arg in (argv or sys.argv[1:]) if not arg.startswith("-")), "cli")
        print_json(failure(command, "invalid_arguments", str(exc)))
        return 1

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    handler = _HANDLERS.get(args.command, _obsidian_handler)
    try:
        envelope = handler(args)
    except CliError as exc:
        envelope = failure(args.command, exc.err_type, exc.message, exc.details)
    except OpenRouterError as exc:
        envelope = failure(args.command, "provider_error", str(exc))
    except settings.ConfigError as exc:
        envelope = failure(args.command, "invalid_arguments", str(exc))
    except Exception as exc:  # noqa: BLE001 - top-level guard -> internal_error envelope
        envelope = failure(args.command, "internal_error", str(exc))

    if envelope.pop("_no_print", False):
        return 0 if envelope.get("ok") else 1

    print_json(envelope)
    return 0 if envelope.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
