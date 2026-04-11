"""OpenRouter-backed embedding, rerank, and chat helpers."""

from __future__ import annotations

import math
import os
import time
from typing import Any, Dict, List, Optional

import httpx
import pandas as pd


class OpenRouterError(RuntimeError):
    """Raised when an OpenRouter request fails."""


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        embedding_model: str,
        chat_model: str,
        rerank_model: Optional[str] = None,
        base_url: str = "https://openrouter.ai/api/v1",
        http_referer: Optional[str] = None,
        app_title: Optional[str] = None,
        timeout_seconds: float = 60.0,
    ):
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required")
        self.api_key = api_key
        self.embedding_model = embedding_model
        self.chat_model = chat_model
        self.rerank_model = rerank_model
        self.base_url = base_url.rstrip("/")
        self.http_referer = http_referer
        self.app_title = app_title
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "OpenRouterClient":
        return cls(
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            embedding_model=os.environ.get(
                "OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-small"
            ),
            chat_model=os.environ.get("OPENROUTER_CHAT_MODEL", "openai/gpt-4o-mini"),
            rerank_model=os.environ.get("OPENROUTER_RERANK_MODEL") or None,
            base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            http_referer=os.environ.get("OPENROUTER_HTTP_REFERER"),
            app_title=os.environ.get("OPENROUTER_APP_TITLE", "Vault RAG"),
        )

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.http_referer:
            headers["HTTP-Referer"] = self.http_referer
        if self.app_title:
            headers["X-Title"] = self.app_title
        return headers

    def _post(self, path: str, payload: Dict[str, Any], retries: int = 3) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        url = f"{self.base_url}/{path.lstrip('/')}"

        for attempt in range(retries):
            try:
                response = httpx.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    timeout=self.timeout_seconds,
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < retries:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                message = exc.response.text.strip() or str(exc)
                if exc.response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < retries:
                    last_error = exc
                    time.sleep(2**attempt)
                    continue
                raise OpenRouterError(f"OpenRouter request failed: {message}") from exc
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 < retries:
                    time.sleep(2**attempt)
                    continue
                raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

        raise OpenRouterError(f"OpenRouter request failed: {last_error}")

    @staticmethod
    def _normalize_embedding(embedding: List[float]) -> List[float]:
        norm = math.sqrt(sum(value * value for value in embedding))
        if norm == 0:
            return embedding
        return [value / norm for value in embedding]

    def embed_texts(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        embeddings: List[List[float]] = []
        for start in range(0, len(texts), batch_size):
            chunk = texts[start : start + batch_size]
            payload = {"model": self.embedding_model, "input": chunk}
            response = self._post("/embeddings", payload)
            data = sorted(response.get("data", []), key=lambda item: item.get("index", 0))
            embeddings.extend(
                self._normalize_embedding(item["embedding"])
                for item in data
                if "embedding" in item
            )
        return embeddings

    def rerank(
        self,
        query: str,
        documents: List[str],
        ids: List[str],
    ) -> pd.DataFrame:
        if not self.rerank_model:
            raise OpenRouterError("No rerank model configured")

        payload = {
            "model": self.rerank_model,
            "query": query,
            "documents": documents,
        }
        response = self._post("/rerank", payload)
        results = response.get("results") or response.get("data") or []
        rows = []
        for result in results:
            index = result.get("index")
            if index is None or index >= len(documents):
                continue
            score = result.get("relevance_score", result.get("score", 0.0))
            rows.append(
                {
                    "id": ids[index],
                    "query": query,
                    "passage": documents[index],
                    "score": float(score),
                }
            )

        if not rows:
            raise OpenRouterError("Rerank response did not contain usable results")

        return pd.DataFrame(rows).set_index("id").sort_values("score", ascending=False)

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        payload = {
            "model": self.chat_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        response = self._post("/chat/completions", payload)
        choices = response.get("choices") or []
        if not choices:
            raise OpenRouterError("Chat response did not contain any choices")
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
            return "".join(text_parts)
        return str(content)
