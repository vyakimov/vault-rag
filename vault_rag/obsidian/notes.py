"""Note mutation commands: create, read, patch, link, move, rename, open.

The backend does the heavy lifting; this module adds dry-run, no-op detection,
collision safety, ambiguity rejection, idempotent link/alias merging, and
data-contract enforcement (`id`/`created` are immutable once set).

Handlers take the parsed argparse namespace and return a full envelope, raising
:class:`~vault_rag.envelope.CliError` for typed failures.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from vault_rag.compounding.backfill_core import now_timestamp
from vault_rag.compounding.lint import WIKILINK_RE
from vault_rag.corpus.chunker import HEADING_RE
from vault_rag.envelope import CliError, success
from vault_rag.obsidian import backend

CONTRACT_IMMUTABLE = ("id", "created")
_PATCH_EMPTY_REJECT = ("", [], None)


# ---------------------------------------------------------------------------
# Minimal frontmatter parsing.
#
# Deliberately NOT vault_rag.corpus.frontmatter: that parser is YAML-typed
# (dates become datetime objects, quotes are normalized), while the mutation
# contract needs untyped, round-trip-faithful values — what is compared and
# written must be exactly what sits in the file.
# ---------------------------------------------------------------------------

def split_note(raw: str) -> Tuple[str, str]:
    """Return (frontmatter_prefix_including_fence_and_newline, body)."""
    if raw.startswith("---\n"):
        lines = raw.split("\n")
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                prefix = "\n".join(lines[: index + 1]) + "\n"
                body = "\n".join(lines[index + 1:])
                return prefix, body
    return "", raw


def parse_frontmatter(raw: str) -> Tuple[Dict[str, Any], List[str]]:
    """Parse simple `key: value` lines and block/inline lists. Returns (fm, warnings)."""
    warnings: List[str] = []
    if not raw.startswith("---\n"):
        return {}, warnings
    lines = raw.split("\n")
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, warnings

    fm: Dict[str, Any] = {}
    i = 1
    while i < end:
        match = re.match(r"^([A-Za-z0-9_\-]+):\s*(.*)$", lines[i])
        if not match:
            if lines[i].strip():
                warnings.append(f"unparsed frontmatter line: {lines[i]!r}")
            i += 1
            continue
        key, value = match.group(1), match.group(2)
        if value == "":
            items = []
            j = i + 1
            while j < end:
                item = re.match(r"^\s*-\s+(.*)$", lines[j])
                if not item:
                    break
                items.append(item.group(1).strip().strip("\"'"))
                j += 1
            if items:
                fm[key] = items
                i = j
                continue
            fm[key] = ""
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            fm[key] = [x.strip().strip("\"'") for x in inner.split(",") if x.strip()] if inner else []
        else:
            fm[key] = value.strip().strip("\"'")
        i += 1
    return fm, warnings


def render_frontmatter(fields: Dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Body helpers (fences, anchors, wikilinks)
# ---------------------------------------------------------------------------

def _fence_line_set(body: str) -> set:
    inside = False
    fenced = set()
    for index, line in enumerate(body.split("\n")):
        if line.strip().startswith("```"):
            inside = not inside
            fenced.add(index)
            continue
        if inside:
            fenced.add(index)
    return fenced


def _replace_anchor(line: str, anchor: str, target: str) -> Tuple[str, bool]:
    replacement = f"[[{target}]]" if anchor == target else f"[[{target}|{anchor}]]"
    i = 0
    while True:
        idx = line.find(anchor, i)
        if idx == -1:
            return line, False
        before = line[:idx]
        if before.count("[[") == before.count("]]"):  # not inside an existing wikilink
            return line[:idx] + replacement + line[idx + len(anchor):], True
        i = idx + len(anchor)


def _bump_updated_if_managed(path: str, changed: bool, dry_run: bool, result: Dict[str, Any]) -> None:
    if not (backend.manage_updated() and changed):
        return
    stamp = now_timestamp()
    result["updated"] = stamp
    if not dry_run:
        backend.run(["property:set", f"path={path}", "name=updated", f"value={stamp}"])


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _resolve_content(args: argparse.Namespace) -> Optional[str]:
    content = getattr(args, "content", None)
    content_file = getattr(args, "content_file", None)
    if content is not None and content_file:
        raise CliError("invalid_arguments", "--content and --content-file are mutually exclusive")
    if content_file:
        if content_file == "-":
            return sys.stdin.read()
        try:
            with open(content_file, "r", encoding="utf-8") as handle:
                return handle.read()
        except FileNotFoundError:
            raise CliError("invalid_arguments", f"file not found: {content_file}")
    if content == "-":
        return sys.stdin.read()
    return content


def cmd_create_note(args: argparse.Namespace) -> Dict[str, Any]:
    path = args.path
    if not path.endswith(".md"):
        raise CliError("invalid_arguments", "path must end with .md")
    if path.startswith("/"):
        raise CliError("invalid_arguments", "path must be vault-relative (no leading /)")

    content = _resolve_content(args)
    frontmatter: Dict[str, Any] = {}
    if args.frontmatter:
        try:
            frontmatter = json.loads(args.frontmatter)
        except json.JSONDecodeError as exc:
            raise CliError("invalid_arguments", f"--frontmatter is not valid JSON: {exc}")
        if not isinstance(frontmatter, dict):
            raise CliError("invalid_arguments", "--frontmatter must be a JSON object")

    if backend.note_exists(path):
        raise CliError("already_exists", f"note already exists: {path}")

    full_text = (render_frontmatter(frontmatter) if frontmatter else "") + (content or "")

    if args.dry_run:
        return success("create-note", {"changed": True, "path": path, "text": full_text},
                       {"dry_run": True})

    out = backend.run(["create", f"path={path}", f"content={backend.escape_for_backend(full_text)}"])
    match = re.search(r"Created:\s*(.+)$", out, re.MULTILINE)
    actual = match.group(1).strip() if match else path
    if actual != path:
        raise CliError("backend_error", "backend created a different path",
                       {"requested": path, "actual": actual})
    return success("create-note", {"changed": True, "path": path}, {"dry_run": False})


def cmd_read_note(args: argparse.Namespace) -> Dict[str, Any]:
    raw = backend.read_note(args.path)
    frontmatter, warnings = parse_frontmatter(raw)
    _, body = split_note(raw)
    result: Dict[str, Any] = {"path": args.path}
    if args.body_only:
        result["body"] = body
    elif args.frontmatter_only:
        result["frontmatter"] = frontmatter
    else:
        result["frontmatter"] = frontmatter
        result["body"] = body
        result["raw"] = raw
    if warnings:
        result["warnings"] = warnings
    return success("read-note", result)


def cmd_merge_frontmatter(args: argparse.Namespace) -> Dict[str, Any]:
    try:
        patch = json.loads(args.patch)
    except json.JSONDecodeError as exc:
        raise CliError("invalid_arguments", f"--patch is not valid JSON: {exc}")
    if not isinstance(patch, dict):
        raise CliError("invalid_arguments", "--patch must be a JSON object")

    raw = backend.read_note(args.path)
    current, _ = parse_frontmatter(raw)

    fields_touched: List[str] = []
    skipped: Dict[str, str] = {}
    scalar_sets: List[Tuple[str, str]] = []
    list_sets: List[Tuple[str, List[Any]]] = []
    diffs: Dict[str, Any] = {}

    for key, value in patch.items():
        if value in _PATCH_EMPTY_REJECT and not isinstance(value, bool):
            raise CliError("invalid_arguments", f"refusing to write empty optional field: {key}")

        if key in CONTRACT_IMMUTABLE and key in current:
            raise CliError("contract_violation", f"{key} is immutable and already set")

        if key == "aliases":
            existing = current.get("aliases") or []
            if isinstance(existing, str):
                existing = [existing]
            new_value: List[Any] = list(existing)
            for item in (value if isinstance(value, list) else [value]):
                if item not in new_value:
                    new_value.append(item)
            if new_value == existing:
                skipped[key] = "already present"
                continue
            list_sets.append((key, new_value))
            diffs[key] = {"current": existing, "proposed": new_value}
            fields_touched.append(key)
            continue

        current_value = current.get(key)
        if isinstance(value, list):
            if current_value == value:
                skipped[key] = "already set"
                continue
            list_sets.append((key, value))
            diffs[key] = {"current": current_value, "proposed": value}
            fields_touched.append(key)
        else:
            if current_value is not None and str(current_value) == str(value):
                skipped[key] = "already set"
                continue
            scalar_sets.append((key, str(value)))
            diffs[key] = {"current": current_value, "proposed": value}
            fields_touched.append(key)

    changed = bool(fields_touched)
    if args.dry_run:
        result = {"changed": changed, "fields_touched": fields_touched, "skipped": skipped, "diffs": diffs}
        _bump_updated_if_managed(args.path, changed, True, result)
        return success("merge-frontmatter", result, {"dry_run": True})

    # Scalars go through property:set on purpose — it writes untyped values,
    # which is what keeps offset-aware timestamps round-tripping verbatim.
    for key, value in scalar_sets:
        backend.run(["property:set", f"path={args.path}", f"name={key}", f"value={value}"])
    # All list fields ride one eval: each processFrontMatter call is a full
    # round-trip to the Obsidian app.
    if list_sets:
        assignments = " ".join(
            f"fm[{json.dumps(key)}] = {json.dumps(value)};" for key, value in list_sets
        )
        code = (
            "(async () => { const f = app.vault.getFileByPath(" + json.dumps(args.path) + "); "
            "if (!f) return 'NOTFOUND'; await app.fileManager.processFrontMatter(f, fm => { "
            + assignments + " }); return 'OK'; })()"
        )
        out = backend.run(["eval", f"code={code}"])
        if "NOTFOUND" in out:
            raise CliError("not_found", f"note not found: {args.path}")

    result = {"changed": changed, "fields_touched": fields_touched, "skipped": skipped}
    _bump_updated_if_managed(args.path, changed, False, result)
    return success("merge-frontmatter", result, {"dry_run": False})


def cmd_add_links(args: argparse.Namespace) -> Dict[str, Any]:
    try:
        links = json.loads(args.links)
    except json.JSONDecodeError as exc:
        raise CliError("invalid_arguments", f"--links is not valid JSON: {exc}")
    if not isinstance(links, list):
        raise CliError("invalid_arguments", "--links must be a JSON array")

    raw = backend.read_note(args.path)
    prefix, body = split_note(raw)
    body_lines = body.split("\n")
    fenced = _fence_line_set(body)

    outcomes: List[Dict[str, Any]] = []
    changed = False

    for link in links:
        target = str(link.get("target", "")).strip()
        anchor = str(link.get("anchor_text", target)).strip() or target
        requested_line = link.get("line")

        if f"[[{target}]]" in body or f"[[{target}|" in body:
            outcomes.append({"target": target, "applied": False, "already": True})
            continue

        candidate_indices: List[int] = []
        if isinstance(requested_line, int) and 1 <= requested_line <= len(body_lines):
            candidate_indices.append(requested_line - 1)
        candidate_indices.extend(i for i in range(len(body_lines)) if i != (requested_line - 1 if isinstance(requested_line, int) else -1))

        applied = False
        for idx in candidate_indices:
            if idx in fenced:
                continue
            new_line, ok = _replace_anchor(body_lines[idx], anchor, target)
            if ok:
                body_lines[idx] = new_line
                outcomes.append({"target": target, "applied": True, "line": idx + 1})
                applied = True
                changed = True
                break
        if not applied:
            outcomes.append({"target": target, "applied": False, "reason": "anchor not found"})

    result: Dict[str, Any] = {"changed": changed, "path": args.path, "links": outcomes}
    if args.dry_run:
        _bump_updated_if_managed(args.path, changed, True, result)
        return success("add-links", result, {"dry_run": True})

    if changed:
        backend.write_body(args.path, prefix + "\n".join(body_lines))
    _bump_updated_if_managed(args.path, changed, False, result)
    return success("add-links", result, {"dry_run": False})


def cmd_insert_related(args: argparse.Namespace) -> Dict[str, Any]:
    try:
        targets = json.loads(args.targets)
    except json.JSONDecodeError as exc:
        raise CliError("invalid_arguments", f"--targets is not valid JSON: {exc}")
    if not isinstance(targets, list):
        raise CliError("invalid_arguments", "--targets must be a JSON array")

    raw = backend.read_note(args.path)
    prefix, body = split_note(raw)
    lines = body.split("\n")
    fenced = _fence_line_set(body)

    related_indices = [
        i for i, line in enumerate(lines)
        if i not in fenced
        and (heading := HEADING_RE.match(line.strip()))
        and heading.group(2).strip().lower() == "related"
    ]
    if len(related_indices) > 1:
        raise CliError("ambiguous_target", "multiple '## Related' headings found")

    existing_targets_lower = set()
    if related_indices:
        start = related_indices[0] + 1
        for line in lines[start:]:
            if HEADING_RE.match(line.strip()):
                break
            bullet = re.match(r"^\s*-\s*(.*)$", line)
            match = WIKILINK_RE.match(bullet.group(1)) if bullet else None
            if match:
                existing_targets_lower.add(match.group(1).strip().lower())

    added: List[str] = []
    already_present: List[str] = []
    for target in targets:
        target = str(target).strip()
        if target.lower() in existing_targets_lower:
            already_present.append(target)
        else:
            added.append(target)
            existing_targets_lower.add(target.lower())

    changed = bool(added)
    result: Dict[str, Any] = {"changed": changed, "path": args.path, "added": added, "already_present": already_present}

    if not changed:
        return success("insert-related", result, {"dry_run": args.dry_run})

    new_bullets = [f"- [[{t}]]" for t in added]
    if related_indices:
        insert_at = related_indices[0] + 1
        while insert_at < len(lines) and lines[insert_at].strip() and not HEADING_RE.match(lines[insert_at].strip()) and lines[insert_at].strip().startswith("-"):
            insert_at += 1
        new_lines = lines[:insert_at] + new_bullets + lines[insert_at:]
        new_body = "\n".join(new_lines)
    else:
        suffix = "" if body.endswith("\n") or body == "" else "\n"
        new_body = body + suffix + "\n## Related\n" + "\n".join(new_bullets) + "\n"

    if args.dry_run:
        _bump_updated_if_managed(args.path, changed, True, result)
        return success("insert-related", result, {"dry_run": True})

    backend.write_body(args.path, prefix + new_body)
    _bump_updated_if_managed(args.path, changed, False, result)
    return success("insert-related", result, {"dry_run": False})


def _parse_destination(out: str, label: str, fallback: str) -> str:
    # Backend prints "Moved: <old> -> <new>" / "Renamed: <old> -> <new>".
    match = re.search(rf"{label}:\s*(.+)$", out, re.MULTILINE)
    if not match:
        return fallback
    tail = match.group(1).strip()
    if "->" in tail:
        return tail.split("->")[-1].strip()
    return tail


def _relocate(action: str, path: str, dest: str, backend_args: List[str],
              label: str, dry_run: bool) -> Dict[str, Any]:
    """Shared move/rename scaffolding: guards, dry-run, backend call, destination parse."""
    if not backend.note_exists(path):
        raise CliError("not_found", f"note not found: {path}")
    if backend.note_exists(dest):
        raise CliError("already_exists", f"destination already exists: {dest}")
    if dry_run:
        return success(action, {"changed": True, "path_before": path, "path_after": dest},
                       {"dry_run": True})
    out = backend.run(backend_args)
    after = _parse_destination(out, label, dest)
    return success(action,
                   {"changed": True, "path_before": path, "path_after": after, "links_updated_by": "obsidian"},
                   {"dry_run": False})


def cmd_move_note(args: argparse.Namespace) -> Dict[str, Any]:
    filename = args.path.rsplit("/", 1)[-1]
    to = args.to.rstrip("/")
    dest = f"{to}/{filename}" if to else filename
    return _relocate("move-note", args.path, dest,
                     ["move", f"path={args.path}", f"to={args.to}"], "Moved", args.dry_run)


def cmd_rename_note(args: argparse.Namespace) -> Dict[str, Any]:
    folder = args.path.rsplit("/", 1)[0] if "/" in args.path else ""
    new_name = args.name if args.name.endswith(".md") else f"{args.name}.md"
    dest = f"{folder}/{new_name}" if folder else new_name
    return _relocate("rename-note", args.path, dest,
                     ["rename", f"path={args.path}", f"name={args.name}"], "Renamed", args.dry_run)


def cmd_open_note(args: argparse.Namespace) -> Dict[str, Any]:
    backend.run(["open", f"path={args.path}"])
    return success("open-note", {"opened": True, "path": args.path})


HANDLERS = {
    "create-note": cmd_create_note,
    "read-note": cmd_read_note,
    "merge-frontmatter": cmd_merge_frontmatter,
    "add-links": cmd_add_links,
    "insert-related": cmd_insert_related,
    "move-note": cmd_move_note,
    "rename-note": cmd_rename_note,
    "open-note": cmd_open_note,
}
