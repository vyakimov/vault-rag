"""JSON envelope helpers (bearctl pattern) for the vault-spider CLI."""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional


class CliError(Exception):
    """Typed failure that maps 1:1 onto a failure envelope."""

    def __init__(self, err_type: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.err_type = err_type
        self.message = message
        self.details = details or {}


def success(
    action: str,
    result: Any = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "action": action,
        "result": result,
        "meta": meta or {},
    }


def failure(
    action: str,
    err_type: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "ok": False,
        "action": action,
        "error": {
            "type": err_type,
            "message": message,
            "details": details or {},
        },
    }


def print_json(payload: Dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    )
    sys.stdout.write("\n")
