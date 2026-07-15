"""Invocation layer for the official Obsidian CLI (the mutation backend).

Every vault mutation goes through the running Obsidian app via the official
`obsidian` binary rather than writing files directly: the backend's move/rename
update incoming wikilinks, its `property:set` preserves unknown frontmatter
keys, and its writes fire vault plugins (notably the modified-date plugin that
owns `updated`). macOS only; the app must be running for every call.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Dict, List, Optional

from vault_rag.envelope import CliError

BINARY_CANDIDATES = [
    "/usr/local/bin/obsidian",
    "/Applications/Obsidian.app/Contents/MacOS/Obsidian",
]
NOISE_RE = re.compile(r"^(Loading updated|Your Obsidian installer)")

# Connection facts for the current invocation; set by configure(). manage_updated
# defaults False because CLI writes DO trigger the modified-date plugin, which
# owns `updated` — patching it here too would double-stamp.
_STATE: Dict[str, Any] = {"binary": None, "vault": None, "manage_updated": False}


def configure(
    binary: Optional[str] = None,
    vault: Optional[str] = None,
    manage_updated: bool = False,
) -> None:
    _STATE.update({"binary": binary, "vault": vault, "manage_updated": manage_updated})


def manage_updated() -> bool:
    return bool(_STATE["manage_updated"])


def _resolve_binary() -> str:
    explicit = _STATE.get("binary")
    if explicit:
        if os.path.exists(explicit):
            return explicit
        raise CliError("invalid_arguments", f"obsidian binary not found: {explicit}")
    for candidate in BINARY_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    raise CliError(
        "invalid_arguments",
        "no obsidian binary found (set --binary or config.yaml `obsidian.binary`)",
    )


def run(args: List[str], timeout: float = 20.0) -> str:
    binary = _resolve_binary()
    argv = [binary]
    if _STATE.get("vault"):
        argv.append(f'vault={_STATE["vault"]}')
    argv.extend(args)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CliError("obsidian_not_running", "Obsidian app must be running")
    except OSError as exc:
        raise CliError("invalid_arguments", f"failed to run obsidian binary: {exc}")

    lines = [line for line in proc.stdout.splitlines() if not NOISE_RE.match(line)]
    cleaned = "\n".join(lines).strip()

    if cleaned == "Vault not found.":
        raise CliError(
            "config_mismatch", f"Obsidian vault not found: {_STATE.get('vault')}"
        )
    if cleaned.startswith("Error:"):
        message = cleaned[len("Error:"):].strip()
        low = message.lower()
        if "not found" in low:
            raise CliError("not_found", message)
        if any(word in low for word in ("vault", "connect", "running", "timeout")):
            raise CliError("obsidian_not_running", "Obsidian app must be running")
        raise CliError("backend_error", message)
    if proc.returncode != 0 and not cleaned:
        raise CliError("obsidian_not_running", "Obsidian app must be running")
    return cleaned


def read_note(path: str) -> str:
    return run(["read", f"path={path}"])


def note_exists(path: str) -> bool:
    try:
        read_note(path)
        return True
    except CliError as exc:
        if exc.err_type == "not_found":
            return False
        raise


def write_body(path: str, content: str) -> None:
    code = (
        "(async () => { const f = app.vault.getFileByPath(" + json.dumps(path) + "); "
        "if (!f) return 'NOTFOUND'; await app.vault.modify(f, " + json.dumps(content) + "); "
        "return 'OK'; })()"
    )
    out = run(["eval", f"code={code}"])
    if "NOTFOUND" in out:
        raise CliError("not_found", f"note not found: {path}")


def escape_for_backend(text: str) -> str:
    # The backend converts literal "\n" sequences in content= to newlines, so
    # escape real backslashes first, then real newlines.
    return text.replace("\\", "\\\\").replace("\n", "\\n")
