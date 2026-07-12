"""JSON-only CLI for Vault RAG: schema, sync, retrieve, synthesize."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import PurePosixPath
from typing import Any, Dict, Optional

from vault_rag.envelope import failure, print_json, success
from vault_rag.llm.openrouter import OpenRouterClient, OpenRouterError

SCHEMA_VERSION = 1


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
            "schema": {"args": {}, "result": "this document"},
            "sync": {
                "args": {"--root": "vault directory (required)", "--reset": "flag"},
                "result": {
                    "added_notes": "int",
                    "updated_notes": "int",
                    "deleted_notes": "int",
                    "unchanged": "int",
                    "total_entries": "int",
                    "warnings": ["str"],
                },
            },
            "retrieve": {
                "args": {
                    "--query": "str (required)",
                    "--mode": "fast|thorough (default fast)",
                    "--granularity": "document|section|mixed (mixed = section pool, max 3 sections per note; documents are not searched)",
                    "-n": "int (default 10)",
                },
                "result": "retrieval_output",
            },
            "synthesize": {
                "args": {
                    "--query": "str",
                    "--mode": "fast|thorough (default thorough)",
                    "--granularity": "document|section|mixed (mixed = section pool, max 3 sections per note; documents are not searched)",
                    "--retrieval": "path to a prior retrieve envelope/contract",
                    "--n-context": "int (default 8)",
                    "--save": "flag: persist a good answer as a distilled note (needs --root, live query)",
                    "--save-dir": "distilled folder relative to --root (default Distilled)",
                    "--root": "vault directory to write the distilled note into",
                },
                "result": "synthesis_output (with embedded retrieval; +saved/saved_path when --save)",
            },
            "lint": {
                "args": {
                    "--root": "vault directory (required)",
                    "--format": "json|text (default json)",
                },
                "result": "lint_report",
            },
            "enrich": {
                "args": {
                    "--root": "corpus directory (required)",
                    "--note": "vault-relative path (xor --stdin)",
                    "--stdin": "flag: enrich raw text from stdin",
                    "--intent": "free text",
                    "--source-type": "transcript|web|pdf|manual",
                    "--source-url": "url",
                    "--title": "known title override",
                },
                "result": "enrichment_plan",
            },
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
                    "broken_wikilinks": "int",
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
        ],
    }


# -- command handlers ---------------------------------------------------------

def cmd_schema(args: argparse.Namespace) -> Dict[str, Any]:
    return success("schema", result=_schema(), meta={"version": SCHEMA_VERSION})


def cmd_sync(args: argparse.Namespace) -> Dict[str, Any]:
    root = args.root
    if not os.path.isdir(root):
        return failure("sync", "invalid_arguments", f"root directory not found: {root}")
    provider = get_provider()
    store = get_store(args.chroma_path, args.collection, provider)
    result = store.sync(root, reset=args.reset)
    return success("sync", result=result, meta={"root": root, "reset": args.reset})


def _run_retrieval(store, provider, query, mode, granularity, n_results):
    from vault_rag.retrieval.evidence import build_retrieval_output
    from vault_rag.retrieval.searcher import Searcher

    searcher = Searcher(store, granularity=granularity, provider=provider)
    result = searcher.hybrid_search(
        query, mode=mode, granularity=granularity, n_results=n_results
    )
    output = build_retrieval_output(query, mode, granularity, result.rows, store)
    return output, result


def cmd_retrieve(args: argparse.Namespace) -> Dict[str, Any]:
    if not args.query or not args.query.strip():
        return failure("retrieve", "invalid_arguments", "--query is required")
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
            store, provider, args.query, args.mode, args.granularity, args.n
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
                store, provider, query, args.mode, args.granularity, args.n
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
    lines = [
        f"Vault lint: {report['root']}",
        f"  notes scanned: {report['notes_scanned']}  ignored: {report['notes_ignored']}",
        "",
        "Summary:",
    ]
    for check, count in report["summary"].items():
        lines.append(f"  {check:<28} {count}")
    for check, entries in report["findings"].items():
        if not entries:
            continue
        lines.append("")
        lines.append(f"{check} (first 20):")
        for entry in entries[:20]:
            lines.append(f"  {entry}")
    return "\n".join(lines)


def cmd_lint(args: argparse.Namespace) -> Dict[str, Any]:
    if not os.path.isdir(args.root):
        return failure("lint", "invalid_arguments", f"root directory not found: {args.root}")

    from vault_rag.compounding.lint import lint_vault

    report = lint_vault(args.root)
    if args.format == "text":
        sys.stdout.write(_lint_text(report) + "\n")
        return {"ok": True, "_no_print": True}
    return success("lint", result=report)


# -- parser -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vault-rag", description="Vault RAG JSON CLI")
    parser.add_argument("--chroma-path", default="chroma_db", help="Chroma persistence dir")
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
    p_sync.add_argument("--root", required=True, help="Vault directory to index")
    p_sync.add_argument("--reset", action="store_true", help="Rebuild from scratch")

    p_retrieve = sub.add_parser("retrieve", parents=[common], help="Retrieve candidate notes")
    p_retrieve.add_argument("--query", required=True)
    p_retrieve.add_argument("--mode", choices=["fast", "thorough"], default="fast")
    p_retrieve.add_argument(
        "--granularity", choices=["document", "section", "mixed"], default="document"
    )
    p_retrieve.add_argument("-n", type=int, default=10)

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
    p_synth.add_argument("--save-dir", dest="save_dir", default="Distilled", help="Distilled note folder (relative to --root)")
    p_synth.add_argument("--root", default=None, help="Vault directory to write the distilled note into")

    p_lint = sub.add_parser("lint", parents=[common], help="Read-only corpus health report")
    p_lint.add_argument("--root", required=True, help="Vault directory to lint")
    p_lint.add_argument("--format", choices=["json", "text"], default="json")

    p_enrich = sub.add_parser(
        "enrich", parents=[common], help="Propose an enrichment plan (no mutations)"
    )
    p_enrich.add_argument("--root", required=True, help="Corpus directory")
    p_enrich.add_argument("--note", default=None, help="Vault-relative path of an existing note")
    p_enrich.add_argument("--stdin", action="store_true", help="Enrich raw text read from stdin")
    p_enrich.add_argument("--intent", default=None)
    p_enrich.add_argument(
        "--source-type", dest="source_type",
        choices=["transcript", "web", "pdf", "manual"], default=None,
    )
    p_enrich.add_argument("--source-url", dest="source_url", default=None)
    p_enrich.add_argument("--title", default=None, help="Known title override")

    return parser


_HANDLERS = {
    "schema": cmd_schema,
    "sync": cmd_sync,
    "retrieve": cmd_retrieve,
    "synthesize": cmd_synthesize,
    "lint": cmd_lint,
    "enrich": cmd_enrich,
}


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    handler = _HANDLERS[args.command]
    try:
        envelope = handler(args)
    except OpenRouterError as exc:
        envelope = failure(args.command, "provider_error", str(exc))
    except Exception as exc:  # noqa: BLE001 - top-level guard -> internal_error envelope
        envelope = failure(args.command, "internal_error", str(exc))

    if envelope.pop("_no_print", False):
        return 0 if envelope.get("ok") else 1

    print_json(envelope)
    return 0 if envelope.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
