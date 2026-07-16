"""Hybrid retrieval over the IndexStore (BM25 + embeddings + optional rerank)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from nltk.stem import PorterStemmer

from vault_spider.config import DEFAULT_SEARCH_PARAMS
from vault_spider.llm.openrouter import OpenRouterClient, OpenRouterError
from vault_spider.retrieval.fusion import (
    min_max_scale,
    reciprocal_rank_fusion,
    zscore_sigmoid_fusion,
)
from vault_spider.retrieval.query_cache import QueryEmbeddingCache
from vault_spider.utils import DEFAULT_STOP_WORDS, normalize_no_punct, tokenize_for_bm25


@dataclass
class RetrievalResult:
    query: str
    mode: str
    granularity: str
    rows: List[Dict[str, object]]
    debug_info: Dict[str, object] = field(default_factory=dict)
    timing_ms: float = 0.0


class Searcher:
    def __init__(
        self,
        store,
        granularity: str = "document",
        provider: Optional[OpenRouterClient] = None,
    ):
        self.store = store
        self.default_granularity = granularity
        self.provider = provider or store.provider
        self.stemmer = PorterStemmer()
        self.stop_words = DEFAULT_STOP_WORDS

    # -- helpers --------------------------------------------------------------

    def calculate_keyword_scores(
        self,
        query: str,
        ids: List[str],
        documents: List[str],
        bm25,
    ) -> pd.Series:
        query_tokens = tokenize_for_bm25(query, self.stop_words, self.stemmer)
        bm25_scores = bm25.get_scores(query_tokens)
        quoted_phrases = re.findall(r'"([^"]*)"', query)
        if not quoted_phrases:
            return pd.Series(
                dict(zip(ids, (float(score) for score in bm25_scores))),
                dtype=float,
                name="keyword_scores",
            )

        keyword_scores: Dict[str, float] = {}
        for doc_id, doc, base_score in zip(ids, documents, bm25_scores):
            doc_no_punct = normalize_no_punct(doc)
            phrase_boost = 0.0
            for phrase in quoted_phrases:
                phrase_norm = normalize_no_punct(phrase)
                if phrase_norm and re.search(
                    rf"(?<!\w){re.escape(phrase_norm)}(?!\w)", doc_no_punct
                ):
                    phrase_boost += 0.3
            keyword_scores[doc_id] = float(base_score) * (1.0 + phrase_boost)
        return pd.Series(keyword_scores, dtype=float, name="keyword_scores")

    def calculate_recency_scores(
        self,
        doc_ids: List[str],
        metadata_by_id: Dict[str, Dict[str, object]],
        decay_days: float = 365.0,
    ) -> pd.Series:
        if not doc_ids:
            return pd.Series(dtype=float)

        recency_scores = {}
        current_date = datetime.now(timezone.utc)
        for doc_id in doc_ids:
            metadata = metadata_by_id.get(doc_id, {})
            raw_date = str(metadata.get("updated") or "") or str(metadata.get("date") or "")
            if not raw_date:
                recency_scores[doc_id] = 1.0
                continue
            try:
                doc_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                if doc_date.tzinfo is None:
                    doc_date = doc_date.replace(tzinfo=timezone.utc)
                age_days = max(0, (current_date - doc_date).days)
                recency_scores[doc_id] = float(np.exp(-age_days / decay_days)) + 1.0
            except ValueError:
                recency_scores[doc_id] = 1.0

        return pd.Series(recency_scores, name="boost_factor", dtype=float)

    # -- main pipeline --------------------------------------------------------

    def _embed_query(self, query: str) -> List[float]:
        chroma_path = getattr(self.store, "chroma_db_path", None)
        if not chroma_path:
            self._query_cache_status = "off"
            return self.provider.embed_texts([query])[0]
        if not hasattr(self, "_query_cache"):
            self._query_cache = QueryEmbeddingCache(
                os.path.join(chroma_path, "query_embedding_cache.json"),
                self.provider.embedding_model,
            )
        cached = self._query_cache.get(query)
        if cached is not None:
            self._query_cache_status = "hit"
            return cached
        embedding = self.provider.embed_texts([query])[0]
        self._query_cache.put(query, embedding)
        self._query_cache_status = "miss"
        return embedding

    def hybrid_search(
        self,
        query: str,
        *,
        mode: str = "fast",
        granularity: Optional[str] = None,
        n_results: Optional[int] = None,
        semantic_weight: Optional[float] = None,
        must_include_terms: Optional[List[str]] = None,
        top_k: Optional[int] = None,
        combine_strategy: Optional[str] = None,
        rrf_k: Optional[int] = None,
        zsigmoid_temperature: Optional[float] = None,
        recency_boost_enabled: Optional[bool] = None,
        recency_weight: Optional[float] = None,
        recency_decay_days: Optional[float] = None,
        folder: Optional[str] = None,
        tags: Optional[List[str]] = None,
        note_type: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> RetrievalResult:
        started = datetime.now(timezone.utc)
        granularity = granularity or self.default_granularity
        if mode not in {"fast", "thorough"}:
            raise ValueError("mode must be fast or thorough")
        if granularity not in {"document", "section", "mixed"}:
            raise ValueError("granularity must be document, section, or mixed")

        params = DEFAULT_SEARCH_PARAMS.with_overrides(
            semantic_weight=semantic_weight,
            top_k=top_k,
            n_results=n_results,
            combine_strategy=combine_strategy,
            rrf_k=rrf_k,
            zsigmoid_temperature=zsigmoid_temperature,
            recency_boost_enabled=recency_boost_enabled,
            recency_weight=recency_weight,
            recency_decay_days=recency_decay_days,
        )
        strategy = params.combine_strategy.lower()

        data_granularity = "section" if granularity == "mixed" else granularity
        documents, ids, metadatas, bm25 = self.store.granularity_data(data_granularity)
        if not ids or bm25 is None:
            raise ValueError("Index is empty for the requested granularity.")

        metadata_by_id = dict(zip(ids, metadatas))
        document_by_id = dict(zip(ids, documents))

        allowed_ids = set(ids)
        if must_include_terms:
            normalized_terms = [
                normalize_no_punct(term)
                for term in must_include_terms
                if normalize_no_punct(term)
            ]
            allowed_ids = {
                doc_id
                for doc_id, document in zip(ids, documents)
                if all(
                    re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalize_no_punct(document))
                    for term in normalized_terms
                )
            }
        since_date = self._parse_filter_date(since, "since")
        until_date = self._parse_filter_date(until, "until")
        if any(value is not None for value in (folder, tags, note_type, since, until)):
            requested_tags = {tag.lower() for tag in tags or []}

            def matches(metadata: Dict[str, object]) -> bool:
                metadata_folder = str(metadata.get("folder", ""))
                if folder and not (
                    metadata_folder == folder
                    or metadata_folder.startswith(folder.rstrip("/") + "/")
                ):
                    return False
                note_tags = {
                    tag.strip().lower()
                    for tag in str(metadata.get("tags", "")).split(",")
                    if tag.strip()
                }
                if not requested_tags.issubset(note_tags):
                    return False
                if note_type and str(metadata.get("note_type", "")).lower() != note_type.lower():
                    return False
                if since_date is not None or until_date is not None:
                    raw = str(metadata.get("updated") or metadata.get("date") or "")
                    try:
                        entry_date = self._parse_filter_date(raw, "entry") if raw else None
                    except ValueError:
                        return False
                    if entry_date is None:
                        return False
                    if since_date is not None and entry_date < since_date:
                        return False
                    if until_date is not None and entry_date > until_date:
                        return False
                return True

            allowed_ids &= {
                doc_id
                for doc_id, metadata in zip(ids, metadatas)
                if matches(metadata)
            }
        if not allowed_ids:
            raise ValueError("No documents match the required filters.")

        query_embedding = self._embed_query(query)
        semantic_results = self.store.collection.query(
            query_embeddings=[query_embedding],
            n_results=min(len(ids), params.top_k),
            where={"granularity": data_granularity},
            include=["distances"],
        )
        semantic_distances = pd.Series(
            semantic_results["distances"][0],
            index=semantic_results["ids"][0],
            name="semantic_distance",
        )
        semantic_scores = pd.Series(
            np.exp(-semantic_distances) + 1.0,
            index=semantic_distances.index,
            name="semantic_scores",
        )

        keyword_scores = self.calculate_keyword_scores(query, ids, documents, bm25)
        top_keyword_scores = keyword_scores.nlargest(
            min(len(keyword_scores), params.top_k)
        )
        # Sorted so downstream stable sorts break score ties deterministically
        # (set iteration order varies across processes).
        candidate_ids = sorted(
            (set(semantic_scores.index) | set(top_keyword_scores.index)) & set(allowed_ids)
        )
        if not candidate_ids:
            raise ValueError("No candidate documents available for the query.")

        raw_scores = pd.DataFrame(index=candidate_ids)
        raw_scores["semantic_scores"] = semantic_scores.reindex(candidate_ids).fillna(0.0)
        raw_scores["keyword_scores"] = keyword_scores.reindex(candidate_ids).fillna(0.0)

        if strategy == "rrf":
            fused = reciprocal_rank_fusion(
                raw_scores["semantic_scores"],
                raw_scores["keyword_scores"],
                allowed_ids=candidate_ids,
                weight=params.semantic_weight,
                k=params.rrf_k,
            )
        elif strategy == "zsigmoid":
            fused = zscore_sigmoid_fusion(
                raw_scores["semantic_scores"],
                raw_scores["keyword_scores"],
                allowed_ids=candidate_ids,
                temperature=params.zsigmoid_temperature,
                weight=params.semantic_weight,
            )
        else:
            fused = pd.DataFrame(index=candidate_ids)
            fused["semantic_score"] = min_max_scale(raw_scores["semantic_scores"])
            fused["keyword_score"] = min_max_scale(raw_scores["keyword_scores"])
            fused["fused_score"] = (
                fused["semantic_score"] * params.semantic_weight
                + fused["keyword_score"] * (1.0 - params.semantic_weight)
            )
            fused = fused.sort_values("fused_score", ascending=False, kind="stable")

        if fused.empty:
            raise ValueError("No candidate documents available for the query.")

        # Rerank only in thorough mode; fast skips it even if a model is configured.
        rerank_ran = False
        fused["reranked_raw_score"] = float("nan")
        fused["reranked_score"] = fused["fused_score"]
        fused["rerank_rank"] = np.nan
        if mode == "thorough" and self.provider.rerank_model:
            rerank_pool_size = min(len(fused), params.rerank_top_k)
            rerank_input = fused.head(rerank_pool_size)
            try:
                reranked = self.provider.rerank(
                    query=query,
                    documents=[document_by_id[doc_id] for doc_id in rerank_input.index],
                    ids=list(rerank_input.index),
                )
                rerank_ran = True
            except OpenRouterError:
                reranked = None

            if rerank_ran and reranked is not None and len(reranked) > 0:
                fused.loc[reranked.index, "reranked_raw_score"] = reranked["score"]
                ordered_ids = list(reranked.sort_values("score", ascending=False).index)
                denom = max(len(ordered_ids) - 1, 1)
                use_ranks = params.rerank_use_ranks
                rank_scores = {}
                rank_positions = {}
                for position, doc_id in enumerate(ordered_ids):
                    rank_positions[doc_id] = position + 1
                    if use_ranks:
                        rank_scores[doc_id] = 1.0 - (position / denom) * 0.5
                    else:
                        rank_scores[doc_id] = float(reranked.loc[doc_id, "score"])
                fused["reranked_score"] = (
                    pd.Series(rank_scores).reindex(fused.index).fillna(fused["fused_score"])
                )
                fused["rerank_rank"] = pd.Series(rank_positions).reindex(fused.index)

        fused["relevance_score"] = fused["reranked_score"]

        if params.recency_boost_enabled:
            recency_boost_factor = (
                self.calculate_recency_scores(
                    list(fused.index), metadata_by_id, params.recency_decay_days
                )
                .reindex(fused.index)
                .fillna(1.0)
            )
            fused["recency_boost_factor"] = recency_boost_factor
            fused["boosted_score"] = (
                fused["relevance_score"] * (1.0 - params.recency_weight)
                + fused["relevance_score"]
                * fused["recency_boost_factor"]
                * params.recency_weight
            )
        else:
            fused["recency_boost_factor"] = 1.0
            fused["boosted_score"] = fused["relevance_score"]

        ordered = fused.sort_values("boosted_score", ascending=False, kind="stable")

        # Assemble output rows, applying the mixed 3-sections-per-note cap.
        rows: List[Dict[str, object]] = []
        per_note: Dict[str, int] = {}
        for doc_id, record in ordered.iterrows():
            metadata = metadata_by_id[doc_id]
            note_id = str(metadata.get("note_id", ""))
            if granularity == "mixed":
                if per_note.get(note_id, 0) >= 3:
                    continue
                per_note[note_id] = per_note.get(note_id, 0) + 1
            rerank_rank = record.get("rerank_rank")
            rows.append(
                {
                    "id": doc_id,
                    "note_id": note_id,
                    "document": document_by_id[doc_id],
                    "metadata": metadata,
                    "bm25": float(raw_scores.loc[doc_id, "keyword_scores"]),
                    "semantic": float(raw_scores.loc[doc_id, "semantic_scores"]),
                    "fused": float(record["fused_score"]),
                    "reranker": (
                        None
                        if not rerank_ran or pd.isna(record.get("reranked_raw_score"))
                        else float(record["reranked_raw_score"])
                    ),
                    "final": float(record["boosted_score"]),
                    "rerank_rank": (
                        None if rerank_rank is None or pd.isna(rerank_rank) else int(rerank_rank)
                    ),
                }
            )
            if len(rows) >= params.n_results:
                break

        elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0
        debug_info = {
            "combine_strategy": strategy,
            "semantic_weight": params.semantic_weight,
            "candidate_pool_size": params.top_k,
            "n_results": params.n_results,
            "recency_boost_enabled": params.recency_boost_enabled,
            "recency_weight": params.recency_weight,
            "recency_decay_days": params.recency_decay_days,
            "rrf_k": params.rrf_k if strategy == "rrf" else None,
            "zsigmoid_temperature": (
                params.zsigmoid_temperature if strategy == "zsigmoid" else None
            ),
            "rerank_enabled": rerank_ran,
            "data_granularity": data_granularity,
            "filters": {
                key: value
                for key, value in {
                    "folder": folder,
                    "tags": tags,
                    "note_type": note_type,
                    "since": since,
                    "until": until,
                    "must_include": must_include_terms,
                }.items()
                if value is not None
            },
            "query_cache": self._query_cache_status,
        }
        return RetrievalResult(
            query=query,
            mode=mode,
            granularity=granularity,
            rows=rows,
            debug_info=debug_info,
            timing_ms=elapsed_ms,
        )

    @staticmethod
    def _parse_filter_date(value: Optional[str], label: str) -> Optional[datetime]:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid --{label} date: {value}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
