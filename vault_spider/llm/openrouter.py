"""OpenRouter-backed embedding, rerank, and chat helpers."""

from __future__ import annotations

import math
import os
import time
from typing import Any, Dict, List, Optional

import httpx
import pandas as pd
from dotenv import load_dotenv

load_dotenv()


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
            app_title=os.environ.get("OPENROUTER_APP_TITLE", "Vault Spider"),
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

    RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

    def _post(self, path: str, payload: Dict[str, Any], retries: int = 3) -> Dict[str, Any]:
        if retries < 1:
            raise ValueError("retries must be at least 1")
        url = f"{self.base_url}/{path.lstrip('/')}"

        for attempt in range(retries):
            try:
                response = httpx.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    timeout=self.timeout_seconds,
                )
            except httpx.HTTPError as exc:
                if attempt + 1 < retries:
                    time.sleep(2**attempt)
                    continue
                raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

            if response.status_code in self.RETRYABLE_STATUSES and attempt + 1 < retries:
                time.sleep(2**attempt)
                continue
            if response.status_code >= 400:
                message = response.text.strip() or f"HTTP {response.status_code}"
                raise OpenRouterError(f"OpenRouter request failed: {message}")
            try:
                response_payload = response.json()
            except ValueError as exc:
                raise OpenRouterError(
                    "OpenRouter returned a non-JSON response"
                ) from exc
            if not isinstance(response_payload, dict):
                raise OpenRouterError("OpenRouter returned JSON that was not an object")
            return response_payload

        raise OpenRouterError("OpenRouter request failed: retries exhausted")

    @staticmethod
    def _normalize_embedding(embedding: List[float]) -> List[float]:
        norm = math.sqrt(sum(value * value for value in embedding))
        if norm == 0 or not math.isfinite(norm):
            raise OpenRouterError("Embedding response contained a non-normalizable vector")
        return [value / norm for value in embedding]

    def embed_texts(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        embeddings: List[List[float]] = []
        embedding_size: Optional[int] = None
        for start in range(0, len(texts), batch_size):
            chunk = texts[start : start + batch_size]
            payload = {"model": self.embedding_model, "input": chunk}
            response = self._post("/embeddings", payload)
            data = response.get("data")
            if not isinstance(data, list):
                raise OpenRouterError("Embedding response data was not an array")

            indexed: Dict[int, List[float]] = {}
            for item in data:
                if not isinstance(item, dict):
                    raise OpenRouterError("Embedding response contained a non-object item")
                index = item.get("index")
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or not 0 <= index < len(chunk)
                ):
                    raise OpenRouterError(f"Embedding response contained invalid index {index!r}")
                if index in indexed:
                    raise OpenRouterError(f"Embedding response contained duplicate index {index}")
                raw_embedding = item.get("embedding")
                if not isinstance(raw_embedding, list) or not raw_embedding:
                    raise OpenRouterError("Embedding response contained an invalid vector")
                try:
                    embedding = [float(value) for value in raw_embedding]
                except (TypeError, ValueError) as exc:
                    raise OpenRouterError(
                        "Embedding response contained a non-numeric vector"
                    ) from exc
                if not all(math.isfinite(value) for value in embedding):
                    raise OpenRouterError("Embedding response contained a non-finite vector")
                if embedding_size is None:
                    embedding_size = len(embedding)
                elif len(embedding) != embedding_size:
                    raise OpenRouterError("Embedding response dimensions were inconsistent")
                indexed[index] = self._normalize_embedding(embedding)

            if len(indexed) != len(chunk):
                # A partial batch would silently misalign embeddings with the
                # documents they are stored against.
                raise OpenRouterError(
                    f"Embedding response returned {len(indexed)} embeddings "
                    f"for {len(chunk)} inputs"
                )
            embeddings.extend(indexed[index] for index in range(len(chunk)))
        return embeddings

    def rerank(
        self,
        query: str,
        documents: List[str],
        ids: List[str],
    ) -> pd.DataFrame:
        if not self.rerank_model:
            raise OpenRouterError("No rerank model configured")
        if len(documents) != len(ids):
            raise ValueError("documents and ids must have the same length")

        payload = {
            "model": self.rerank_model,
            "query": query,
            "documents": documents,
        }
        response = self._post("/rerank", payload)
        results = response.get("results") or response.get("data") or []
        if not isinstance(results, list):
            raise OpenRouterError("Rerank response results were not an array")
        rows = []
        seen_indexes = set()
        for result in results:
            if not isinstance(result, dict):
                raise OpenRouterError("Rerank response contained a non-object item")
            index = result.get("index")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or not 0 <= index < len(documents)
            ):
                raise OpenRouterError(f"Rerank response contained invalid index {index!r}")
            if index in seen_indexes:
                raise OpenRouterError(f"Rerank response contained duplicate index {index}")
            seen_indexes.add(index)
            score = result.get("relevance_score", result.get("score", 0.0))
            try:
                score = float(score)
            except (TypeError, ValueError) as exc:
                raise OpenRouterError("Rerank response contained a non-numeric score") from exc
            if not math.isfinite(score):
                raise OpenRouterError("Rerank response contained a non-finite score")
            rows.append(
                {
                    "id": ids[index],
                    "query": query,
                    "passage": documents[index],
                    "score": score,
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
        model: Optional[str] = None,
    ) -> str:
        payload = {
            "model": model or self.chat_model,
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
        if not isinstance(choices, list) or not choices:
            raise OpenRouterError("Chat response did not contain any choices")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise OpenRouterError("Chat response contained an invalid choice")
        message = first_choice.get("message", {}) or {}
        if not isinstance(message, dict):
            raise OpenRouterError("Chat response contained an invalid message")
        content = message.get("content")
        if isinstance(content, list):
            text_parts = [
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            ]
            content = "".join(text_parts)
        # Reasoning models sometimes return content=None when the max_tokens
        # budget gets absorbed by internal reasoning. Fall back to the
        # reasoning field so downstream parsing has something to work with.
        if content is None or content == "":
            content = message.get("reasoning") or ""
        return str(content)
