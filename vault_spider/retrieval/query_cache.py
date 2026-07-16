"""Best-effort on-disk cache for query embeddings."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Dict, List, Optional


class QueryEmbeddingCache:
    def __init__(self, path: str, model: str, max_entries: int = 256):
        self.path = path
        self.model = model
        self.max_entries = max_entries
        self._loaded = False
        self._entries: Dict[str, Dict[str, object]] = {}

    @staticmethod
    def _key(query: str) -> str:
        return hashlib.sha256(query.encode("utf-8")).hexdigest()

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("model") != self.model or not isinstance(
                payload.get("entries"), dict
            ):
                return
            self._entries = payload["entries"]
        except (OSError, ValueError, TypeError, AttributeError):
            self._entries = {}

    def get(self, query: str) -> Optional[List[float]]:
        try:
            self._load()
            entry = self._entries.get(self._key(query))
            if not isinstance(entry, dict) or not isinstance(entry.get("embedding"), list):
                return None
            return [float(value) for value in entry["embedding"]]
        except (OSError, ValueError, TypeError):
            return None

    def put(self, query: str, embedding: List[float]) -> None:
        try:
            self._load()
            self._entries[self._key(query)] = {
                "embedding": [float(value) for value in embedding],
                "ts": time.time(),
            }
            if len(self._entries) > self.max_entries:
                oldest = sorted(
                    self._entries,
                    key=lambda key: float(self._entries[key].get("ts", 0.0)),
                )[: len(self._entries) - self.max_entries]
                for key in oldest:
                    del self._entries[key]
            temporary = self.path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"model": self.model, "entries": self._entries}, handle)
            os.replace(temporary, self.path)
        except (OSError, ValueError, TypeError):
            return
