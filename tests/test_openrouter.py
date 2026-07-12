"""Tests for vault_rag.llm.openrouter request/response hardening (no network)."""

from __future__ import annotations

import httpx
import pytest

from vault_rag.llm import openrouter
from vault_rag.llm.openrouter import OpenRouterClient, OpenRouterError


def make_client() -> OpenRouterClient:
    return OpenRouterClient(api_key="key", embedding_model="e", chat_model="c")


def test_missing_api_key_raises():
    with pytest.raises(ValueError):
        OpenRouterClient(api_key="", embedding_model="e", chat_model="c")


def test_post_raises_on_non_json_body(monkeypatch):
    client = make_client()
    monkeypatch.setattr(
        httpx, "post", lambda *args, **kwargs: httpx.Response(200, text="<html>oops</html>")
    )
    with pytest.raises(OpenRouterError, match="non-JSON"):
        client._post("/embeddings", {})


def test_post_surfaces_body_after_retryable_statuses_exhaust(monkeypatch):
    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return httpx.Response(502, text="bad gateway")

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(openrouter.time, "sleep", lambda seconds: None)

    client = make_client()
    with pytest.raises(OpenRouterError, match="bad gateway"):
        client._post("/embeddings", {}, retries=3)
    assert calls["n"] == 3


def test_post_raises_immediately_on_non_retryable_status(monkeypatch):
    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return httpx.Response(401, text="unauthorized")

    monkeypatch.setattr(httpx, "post", fake_post)

    client = make_client()
    with pytest.raises(OpenRouterError, match="unauthorized"):
        client._post("/chat/completions", {})
    assert calls["n"] == 1


def test_embed_texts_rejects_partial_batches(monkeypatch):
    client = make_client()
    monkeypatch.setattr(
        client,
        "_post",
        lambda path, payload: {"data": [{"index": 0, "embedding": [1.0, 0.0]}]},
    )
    with pytest.raises(OpenRouterError, match="2 inputs"):
        client.embed_texts(["first", "second"])
