"""Hybrid retrieval over the IndexStore (BM25 + embeddings + optional rerank)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd
from nltk.stem import PorterStemmer

from vault_rag.config import SEARCH_CONFIG
from vault_rag.llm.openrouter import OpenRouterClient, OpenRouterError
from vault_rag.retrieval.fusion import (
    min_max_scale,
    reciprocal_rank_fusion,
    zscore_sigmoid_fusion,
)
from vault_rag.utils import DEFAULT_STOP_WORDS, normalize_no_punct, tokenize_for_bm25


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

    def extract_important_terms(self, query: str) -> Tuple[Set[str], Set[str], List[str]]:
        quoted_phrases = re.findall(r'"([^"]*)"', query)
        clean_query = query
        for phrase in quoted_phrases:
            clean_query = clean_query.replace(f'"{phrase}"', "")
        terms = set(clean_query.lower().split())
        stemmed_terms = {self.stemmer.stem(term) for term in terms}
        return terms, stemmed_terms, quoted_phrases

    def calculate_keyword_scores(
        self,
        query: str,
        ids: List[str],
        documents: List[str],
        bm25,
    ) -> pd.Series:
        query_tokens = tokenize_for_bm25(query, self.stop_words, self.stemmer)
        bm25_scores = bm25.get_scores(query_tokens)
        _, _, quoted_phrases = self.extract_important_terms(query)

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
    ) -> RetrievalResult:
        started = datetime.now(timezone.utc)
        granularity = granularity or self.default_granularity
        data_granularity = "section" if granularity == "mixed" else granularity
        documents, ids, metadatas, bm25 = self.store.granularity_data(data_granularity)
        if not ids or bm25 is None:
            raise ValueError("Index is empty for the requested granularity.")

        metadata_by_id = dict(zip(ids, metadatas))
        document_by_id = dict(zip(ids, documents))

        semantic_wt = (
            float(SEARCH_CONFIG.get("semantic_weight", 0.5))
            if semantic_weight is None
            else semantic_weight
        )
        n_res = int(SEARCH_CONFIG.get("n_results", 10)) if n_results is None else n_results
        candidate_pool_size = (
            int(SEARCH_CONFIG.get("default_top_k", 150)) if top_k is None else top_k
        )
        strategy = (
            str(SEARCH_CONFIG.get("combine_strategy", "rrf"))
            if combine_strategy is None
            else combine_strategy
        ).lower()
        rrf_k_val = int(SEARCH_CONFIG.get("rrf_k", 60)) if rrf_k is None else rrf_k
        zsig_temp = (
            float(SEARCH_CONFIG.get("zsigmoid_temperature", 1.0))
            if zsigmoid_temperature is None
            else zsigmoid_temperature
        )
        use_recency = (
            bool(SEARCH_CONFIG.get("recency_boost_enabled", True))
            if recency_boost_enabled is None
            else recency_boost_enabled
        )
        recency_wt = (
            float(SEARCH_CONFIG.get("recency_weight", 0.2))
            if recency_weight is None
            else recency_weight
        )
        decay_days = (
            float(SEARCH_CONFIG.get("recency_decay_days", 365.0))
            if recency_decay_days is None
            else recency_decay_days
        )

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
        if not allowed_ids:
            raise ValueError("No documents match the required terms.")

        query_embedding = self.provider.embed_texts([query])[0]
        semantic_results = self.store.collection.query(
            query_embeddings=[query_embedding],
            n_results=min(len(ids), candidate_pool_size),
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
            min(len(keyword_scores), candidate_pool_size)
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
                weight=semantic_wt,
                k=rrf_k_val,
            )
        elif strategy == "zsigmoid":
            fused = zscore_sigmoid_fusion(
                raw_scores["semantic_scores"],
                raw_scores["keyword_scores"],
                allowed_ids=candidate_ids,
                temperature=zsig_temp,
                weight=semantic_wt,
            )
        else:
            fused = pd.DataFrame(index=candidate_ids)
            fused["semantic_score"] = min_max_scale(raw_scores["semantic_scores"])
            fused["keyword_score"] = min_max_scale(raw_scores["keyword_scores"])
            fused["fused_score"] = (
                fused["semantic_score"] * semantic_wt
                + fused["keyword_score"] * (1.0 - semantic_wt)
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
            rerank_pool_size = min(len(fused), int(SEARCH_CONFIG.get("rerank_top_k", 30)))
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
                use_ranks = bool(SEARCH_CONFIG.get("rerank_use_ranks", True))
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

        if use_recency:
            recency_boost_factor = (
                self.calculate_recency_scores(list(fused.index), metadata_by_id, decay_days)
                .reindex(fused.index)
                .fillna(1.0)
            )
            fused["recency_boost_factor"] = recency_boost_factor
            fused["boosted_score"] = (
                fused["relevance_score"] * (1.0 - recency_wt)
                + fused["relevance_score"] * fused["recency_boost_factor"] * recency_wt
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
            if len(rows) >= n_res:
                break

        elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0
        debug_info = {
            "combine_strategy": strategy,
            "semantic_weight": semantic_wt,
            "candidate_pool_size": candidate_pool_size,
            "n_results": n_res,
            "recency_boost_enabled": use_recency,
            "recency_weight": recency_wt,
            "recency_decay_days": decay_days,
            "rrf_k": rrf_k_val if strategy == "rrf" else None,
            "zsigmoid_temperature": zsig_temp if strategy == "zsigmoid" else None,
            "rerank_enabled": rerank_ran,
            "data_granularity": data_granularity,
        }
        return RetrievalResult(
            query=query,
            mode=mode,
            granularity=granularity,
            rows=rows,
            debug_info=debug_info,
            timing_ms=elapsed_ms,
        )
