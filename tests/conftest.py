"""Shared pytest fixtures: a network-free FakeProvider and a tiny vault."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd
import pytest

from vault_spider import settings
from vault_spider.obsidian import registry

EMBED_DIM = 16


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch):
    """Point settings at a temp config.yaml and reset the cache around the test."""
    monkeypatch.setenv("VAULT_SPIDER_CONFIG", str(tmp_path / "config.yaml"))
    settings.reset()
    yield tmp_path
    monkeypatch.delenv("VAULT_SPIDER_CONFIG", raising=False)
    settings.reset()


@pytest.fixture(autouse=True)
def isolated_default_config(tmp_path: Path, monkeypatch, request):
    """Keep tests that do not request a config fixture independent of the user's config."""
    if "isolated_config" in request.fixturenames:
        yield
        return
    monkeypatch.setenv("VAULT_SPIDER_CONFIG", str(tmp_path / "default-config.yaml"))
    settings.reset()
    yield
    settings.reset()


@pytest.fixture(autouse=True)
def isolated_obsidian_registry(tmp_path: Path, monkeypatch):
    """Prevent a developer's real Obsidian registry from affecting tests."""
    path = tmp_path / "obsidian.json"
    monkeypatch.setattr(registry, "registry_path", lambda: path)
    yield path


def write_registry(path: Path, vaults) -> None:
    """Write a minimal Obsidian registry fixture."""
    path.write_text(json.dumps({"vaults": vaults, "cli": True}), encoding="utf-8")


def write_config(tmp_path: Path, text: str) -> None:
    """Write config.yaml and invalidate the cache — parsing happens on next read."""
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    settings.reset()


class FakeProvider:
    """Deterministic, network-free stand-in for OpenRouterClient."""

    def __init__(self, rerank_model: str | None = "fake-rerank"):
        self.embedding_model = "fake-embed"
        self.chat_model = "fake-chat"
        self.rerank_model = rerank_model
        # Records the texts passed to each embed_texts call.
        self.embed_calls: List[List[str]] = []
        # Canned chat reply (JSON string); tests override as needed.
        self.chat_response = json.dumps(
            {"answer": "Canned.", "citations": ["S0"], "confidence": "High", "abstained": False}
        )

    def _unit_vector(self, text: str) -> List[float]:
        seed = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16) % (2**32)
        rng = np.random.RandomState(seed)
        vec = rng.rand(EMBED_DIM)
        norm = np.linalg.norm(vec) or 1.0
        return (vec / norm).tolist()

    def embed_texts(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        self.embed_calls.append(list(texts))
        return [self._unit_vector(text) for text in texts]

    def rerank(self, query: str, documents: List[str], ids: List[str]) -> pd.DataFrame:
        # Reversed input order with descending fake scores.
        reversed_ids = list(reversed(ids))
        rows = [
            {"id": doc_id, "score": float(len(reversed_ids) - position)}
            for position, doc_id in enumerate(reversed_ids)
        ]
        return pd.DataFrame(rows).set_index("id").sort_values("score", ascending=False)

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.2,
             max_tokens: int = 1024, model: str | None = None) -> str:
        return self.chat_response


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


NOTE_BIG_TOKEN = "zqxq"


def _big_body() -> str:
    # ~20k chars across many short lines, no headings -> splits into several windows.
    lines = [
        f"Line {i} carries the {NOTE_BIG_TOKEN} marker and some filler words here."
        for i in range(400)
    ]
    return "\n".join(lines)


@pytest.fixture
def tiny_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()

    (vault / "note_a.md").write_text(
        "---\n"
        "title: Alpha note\n"
        "type: recipe\n"
        "date: 2024-01-10\n"
        "tags: [alpha, notes]\n"
        "---\n"
        "Intro preamble about alpha.\n\n"
        "# Overview\n"
        "Alpha overview paragraph.\n\n"
        "## Details\n"
        "Alpha details paragraph.\n\n"
        "#### Sub detail\n"
        "This h4 stays inside Details.\n",
        encoding="utf-8",
    )

    (vault / "note_plain.md").write_text(
        "Just a plain body with no frontmatter and no headings about beta.\n",
        encoding="utf-8",
    )

    (vault / "note_updated.md").write_text(
        "---\n"
        "title: Updated note\n"
        "created: 2023-05-01\n"
        "updated: 2025-06-15\n"
        "tags: gamma\n"
        "---\n"
        "# Gamma\n"
        "Gamma content that was recently updated.\n",
        encoding="utf-8",
    )

    (vault / "note_code.md").write_text(
        "---\ntitle: Code note\n---\n"
        "# Real heading\n"
        "Some text.\n\n"
        "```python\n"
        "# not a heading\n"
        "x = 1\n"
        "```\n"
        "More text after the fence.\n",
        encoding="utf-8",
    )

    (vault / "note_secret.md").write_text(
        "---\ntitle: Secret note\n---\n"
        "This note is #secret and must be skipped.\n",
        encoding="utf-8",
    )

    (vault / "note_big.md").write_text(
        "---\ntitle: Big note\ndate: 2024-03-01\n---\n" + _big_body() + "\n",
        encoding="utf-8",
    )

    return vault
