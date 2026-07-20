"""MCP server exposing Vault Spider's stable JSON CLI contracts.

The server deliberately delegates each tool call to the CLI in a subprocess. That keeps
argument validation, error envelopes, provider setup, and the Obsidian-only mutation boundary
in one place while also isolating concurrent MCP calls from the mutation backend's process state.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
READ_ONLY_NETWORK = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
MUTATING = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)
MUTATING_NETWORK = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)
NONDESTRUCTIVE_SIDE_EFFECT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

Mode = Literal["fast", "thorough"]
Granularity = Literal["document", "section", "mixed"]
Provenance = Literal["human", "reference", "llm", "distilled"]
NoteView = Literal["full", "frontmatter", "body"]
ResultLimit = Annotated[int, Field(ge=1, le=50)]
ContextLimit = Annotated[int, Field(ge=1, le=25)]


class LinkSpec(BaseModel):
    """A requested wikilink insertion."""

    target: str
    anchor_text: Optional[str] = None
    line: Optional[int] = Field(default=None, ge=1)


class TextEdit(BaseModel):
    """One exact-text replacement in a note body."""

    old_text: str = Field(min_length=1)
    new_text: str
    occurrence: Optional[int] = Field(default=None, ge=1)


_cli_prefix: list[str] = []


def _failure(action: str, message: str, details: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    error: dict[str, Any] = {"type": "internal_error", "message": message}
    if details:
        error["details"] = details
    return {"ok": False, "action": action, "error": error}


def _run_cli(arguments: list[str]) -> dict[str, Any]:
    """Run one CLI command and return its JSON envelope, including typed failures."""
    action = next((arg for arg in arguments if not arg.startswith("-")), "mcp")
    completed = subprocess.run(
        [sys.executable, "-m", "vault_spider.cli", *_cli_prefix, *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return _failure(
            action,
            "vault-spider returned invalid JSON",
            {
                "exit_code": completed.returncode,
                "stderr": completed.stderr.strip(),
            },
        )
    if not isinstance(envelope, dict):
        return _failure(action, "vault-spider returned a non-object JSON value")
    return envelope


def _add_optional(arguments: list[str], flag: str, value: Any) -> None:
    if value is not None:
        arguments.extend([flag, str(value)])


def _add_filters(
    arguments: list[str],
    *,
    folder: Optional[str],
    tags: Optional[list[str]],
    note_type: Optional[str],
    provenance: Optional[Provenance],
    since: Optional[str],
    until: Optional[str],
    must_include: Optional[list[str]],
) -> None:
    _add_optional(arguments, "--folder", folder)
    for tag in tags or []:
        arguments.extend(["--tag", tag])
    _add_optional(arguments, "--type", note_type)
    _add_optional(arguments, "--provenance", provenance)
    _add_optional(arguments, "--since", since)
    _add_optional(arguments, "--until", until)
    for term in must_include or []:
        arguments.extend(["--must-include", term])


def vault_stats() -> dict[str, Any]:
    """Return statistics for the current Vault Spider index."""
    return _run_cli(["stats"])


def sync_index(
    root: Optional[str] = None,
    reset: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan or run an incremental vault index sync. Defaults to a non-writing dry run."""
    arguments = ["sync"]
    _add_optional(arguments, "--root", root)
    if reset:
        arguments.append("--reset")
    if dry_run:
        arguments.append("--dry-run")
    return _run_cli(arguments)


def search_vault(
    query: str,
    mode: Mode = "fast",
    granularity: Granularity = "document",
    limit: ResultLimit = 10,
    folder: Optional[str] = None,
    tags: Optional[list[str]] = None,
    note_type: Optional[str] = None,
    provenance: Optional[Provenance] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    must_include: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Hybrid-search indexed notes and return ranked candidates with score evidence."""
    arguments = [
        "retrieve",
        "--query",
        query,
        "--mode",
        mode,
        "--granularity",
        granularity,
        "-n",
        str(limit),
    ]
    _add_filters(
        arguments,
        folder=folder,
        tags=tags,
        note_type=note_type,
        provenance=provenance,
        since=since,
        until=until,
        must_include=must_include,
    )
    return _run_cli(arguments)


def answer_from_vault(
    query: str,
    mode: Mode = "thorough",
    granularity: Granularity = "mixed",
    limit: ResultLimit = 10,
    context_notes: ContextLimit = 8,
    folder: Optional[str] = None,
    tags: Optional[list[str]] = None,
    note_type: Optional[str] = None,
    provenance: Optional[Provenance] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    must_include: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Answer from indexed notes with citations, abstaining when evidence is insufficient."""
    arguments = [
        "synthesize",
        "--query",
        query,
        "--mode",
        mode,
        "--granularity",
        granularity,
        "-n",
        str(limit),
        "--n-context",
        str(context_notes),
    ]
    _add_filters(
        arguments,
        folder=folder,
        tags=tags,
        note_type=note_type,
        provenance=provenance,
        since=since,
        until=until,
        must_include=must_include,
    )
    return _run_cli(arguments)


def lint_vault(root: Optional[str] = None) -> dict[str, Any]:
    """Inspect vault health without modifying notes."""
    arguments = ["lint", "--format", "json"]
    _add_optional(arguments, "--root", root)
    return _run_cli(arguments)


def plan_enrichment(
    note: str,
    root: Optional[str] = None,
    intent: Optional[str] = None,
    source_url: Optional[str] = None,
    title: Optional[str] = None,
) -> dict[str, Any]:
    """Propose metadata, links, and placement for an existing note without changing it."""
    arguments = ["enrich", "--note", note]
    _add_optional(arguments, "--root", root)
    _add_optional(arguments, "--intent", intent)
    _add_optional(arguments, "--source-url", source_url)
    _add_optional(arguments, "--title", title)
    return _run_cli(arguments)


def read_note(path: str, view: NoteView = "full") -> dict[str, Any]:
    """Read one vault-relative Markdown note through the running Obsidian app."""
    arguments = ["read-note", "--path", path]
    if view == "frontmatter":
        arguments.append("--frontmatter-only")
    elif view == "body":
        arguments.append("--body-only")
    return _run_cli(arguments)


def edit_note(
    path: str,
    edits: Annotated[list[TextEdit], Field(min_length=1)],
    expected_sha256: Optional[str] = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Edit a note body with exact replacements and an optimistic concurrency guard.

    First call with dry_run=true and review result.diff. To apply exactly that preview, call
    again with identical edits, dry_run=false, and expected_sha256 from the dry-run result.
    The apply fails if any part of the note changed after preview.
    """
    payload = [edit.model_dump(exclude_none=True) for edit in edits]
    arguments = ["edit-note", "--path", path, "--edits", json.dumps(payload)]
    _add_optional(arguments, "--expected-sha256", expected_sha256)
    if dry_run:
        arguments.append("--dry-run")
    return _run_cli(arguments)


def create_note(
    path: str,
    content: str = "",
    frontmatter: Optional[dict[str, Any]] = None,
    auto_id: bool = True,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Create a note through Obsidian. Defaults to dry-run and refuses overwrites."""
    arguments = ["create-note", "--path", path, "--content", content]
    if frontmatter is not None:
        arguments.extend(["--frontmatter", json.dumps(frontmatter)])
    if auto_id:
        arguments.append("--auto-id")
    if dry_run:
        arguments.append("--dry-run")
    return _run_cli(arguments)


def merge_frontmatter(
    path: str,
    patch: dict[str, Any],
    dry_run: bool = True,
) -> dict[str, Any]:
    """Merge frontmatter through Obsidian. Defaults to dry-run; id and created stay immutable."""
    arguments = [
        "merge-frontmatter",
        "--path",
        path,
        "--patch",
        json.dumps(patch),
    ]
    if dry_run:
        arguments.append("--dry-run")
    return _run_cli(arguments)


def add_links(
    path: str,
    links: list[LinkSpec],
    dry_run: bool = True,
) -> dict[str, Any]:
    """Turn matching note text into wikilinks through Obsidian. Defaults to dry-run."""
    payload = [link.model_dump(exclude_none=True) for link in links]
    arguments = ["add-links", "--path", path, "--links", json.dumps(payload)]
    if dry_run:
        arguments.append("--dry-run")
    return _run_cli(arguments)


def insert_related(
    path: str,
    targets: list[str],
    dry_run: bool = True,
) -> dict[str, Any]:
    """Add deduplicated wikilinks under a Related heading. Defaults to dry-run."""
    arguments = [
        "insert-related",
        "--path",
        path,
        "--targets",
        json.dumps(targets),
    ]
    if dry_run:
        arguments.append("--dry-run")
    return _run_cli(arguments)


def move_note(path: str, destination: str, dry_run: bool = True) -> dict[str, Any]:
    """Move a note through Obsidian so incoming links update. Defaults to dry-run."""
    arguments = ["move-note", "--path", path, "--to", destination]
    if dry_run:
        arguments.append("--dry-run")
    return _run_cli(arguments)


def rename_note(path: str, name: str, dry_run: bool = True) -> dict[str, Any]:
    """Rename a note through Obsidian so incoming links update. Defaults to dry-run."""
    arguments = ["rename-note", "--path", path, "--name", name]
    if dry_run:
        arguments.append("--dry-run")
    return _run_cli(arguments)


def open_note(path: str) -> dict[str, Any]:
    """Open a vault-relative note in the running Obsidian app."""
    return _run_cli(["open-note", "--path", path])


def create_server(*, host: str = "127.0.0.1", port: int = 8000) -> FastMCP:
    """Build the MCP server for stdio or Streamable HTTP transport."""
    server = FastMCP(
        "Vault Spider",
        instructions=(
            "Use these tools to search, answer from, inspect, and safely modify the configured "
            "Obsidian vault. Every result is a Vault Spider JSON envelope: check `ok` before "
            "using `result`. Mutation tools default to dry-run; only set dry_run=false after "
            "reviewing the proposed changes with the user."
        ),
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
    )
    registrations = [
        (vault_stats, READ_ONLY),
        (sync_index, MUTATING_NETWORK),
        (search_vault, READ_ONLY_NETWORK),
        (answer_from_vault, READ_ONLY_NETWORK),
        (lint_vault, READ_ONLY),
        (plan_enrichment, READ_ONLY_NETWORK),
        (read_note, READ_ONLY),
        (edit_note, MUTATING),
        (create_note, MUTATING),
        (merge_frontmatter, MUTATING),
        (add_links, MUTATING),
        (insert_related, MUTATING),
        (move_note, MUTATING),
        (rename_note, MUTATING),
        (open_note, NONDESTRUCTIVE_SIDE_EFFECT),
    ]
    for function, tool_annotations in registrations:
        server.tool(annotations=tool_annotations, structured_output=True)(function)
    return server


mcp = create_server()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Expose Vault Spider over MCP")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="stdio for local clients; streamable-http for remote clients",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8000, help="HTTP bind port")
    parser.add_argument("--chroma-path", default=None, help="Override the configured Chroma path")
    parser.add_argument("--collection", default=None, help="Override the Chroma collection")
    return parser


def main(argv: Optional[list[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    global _cli_prefix
    _cli_prefix = []
    chroma_path = args.chroma_path
    if chroma_path is not None:
        expanded = Path(chroma_path).expanduser()
        chroma_path = str(expanded if expanded.is_absolute() else Path.cwd() / expanded)
    _add_optional(_cli_prefix, "--chroma-path", chroma_path)
    _add_optional(_cli_prefix, "--collection", args.collection)
    server = create_server(host=args.host, port=args.port)
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
