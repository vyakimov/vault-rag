"""Pure text/hashing helpers shared across the package."""

from __future__ import annotations

import ctypes
import hashlib
import re
import string
from pathlib import PurePosixPath
from typing import Iterable, List

WORD_RE = r"\b\w+\b"

# Small built-in list so the app does not depend on downloading NLTK corpora.
DEFAULT_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "he",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "that",
    "the",
    "to",
    "was",
    "were",
    "will",
    "with",
}

conversion_d = {
    idx: value for value, idx in zip(string.digits + string.ascii_letters, range(62))
}


def decimal_to_base(n: int) -> str:
    if n == 0:
        return "0"

    digits = []
    while n:
        digits.append(int(n % 62))
        n //= 62

    return "".join(conversion_d[x] for x in reversed(digits))


def hash_string(value: str) -> str:
    return decimal_to_base(
        ctypes.c_uint64(int(hashlib.md5(value.encode("utf-8")).hexdigest(), 16)).value
    )


def normalize_no_punct(text: str) -> str:
    return " ".join(re.findall(WORD_RE, text.lower()))


def tokenize_for_bm25(text: str, stop_words: Iterable[str], stemmer) -> List[str]:
    tokens = re.findall(WORD_RE, text.lower())
    return [stemmer.stem(token) for token in tokens if token and token not in stop_words]


def validate_vault_relative_path(value: str, *, label: str = "path") -> str:
    """Return a canonical vault-relative POSIX path or raise ``ValueError``.

    Obsidian paths are always slash-separated and relative to the active vault.
    Rejecting ambiguous segments here prevents callers from accidentally escaping
    a configured vault root when the same path is used with the filesystem.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL bytes")
    if "\\" in value:
        raise ValueError(f"{label} must use '/' as the path separator")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError(f"{label} must be vault-relative")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"{label} must not contain empty, '.' or '..' segments")
    return path.as_posix()
