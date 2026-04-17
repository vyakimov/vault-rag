from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Collection, Dict, List, Optional, Set, Tuple, TypedDict

import numpy as np
import pandas as pd
from nltk.stem import PorterStemmer

try:
    from scripts.config import SEARCH_CONFIG
    from scripts.openrouter_client import OpenRouterClient, OpenRouterError
    from scripts.utils import DEFAULT_STOP_WORDS, normalize_no_punct, pretty_print as pp
    from scripts.utils import tokenize_for_bm25
except ImportError:
    from config import SEARCH_CONFIG
    from openrouter_client import OpenRouterClient, OpenRouterError
    from utils import DEFAULT_STOP_WORDS, normalize_no_punct, pretty_print as pp
    from utils import tokenize_for_bm25

TESTING = False


class DebugInfo(TypedDict, total=False):
    query: str
    combine_strategy: str
    semantic_weight: float
    recency_boost_enabled: bool
    recency_weight: float
    recency_decay_days: float
    rrf_k: Optional[int]
    zsigmoid_temperature: Optional[float]
    rerank_enabled: bool
    rerank_use_ranks: bool
    judge_enabled: bool
    judge_top_k: int
    judge_votes: int
    judge_model: Optional[str]


class HybridSearchResult(TypedDict, total=False):
    ids: List[str]
    relevant: List[bool]
    documents: List[str]
    metadatas: List[Dict[str, str]]
    semantic_scores: List[float]
    keyword_scores: List[float]
    fused_scores: List[float]
    reranked_scores: List[float]
    reranked_raw_scores: List[float]
    judge_scores: List[float]
    judge_raw_scores: List[Optional[float]]
    judge_votes: List[Optional[List[int]]]
    judge_grades: List[Optional[str]]
    judge_reasonings: List[str]
    boosted_scores: List[float]
    recency_boost_factor: List[float]
    debug_info: DebugInfo
    rrf_semantic_scores: List[float]
    rrf_keyword_scores: List[float]
    zsigmoid_semantic_scores: List[float]
    zsigmoid_keyword_scores: List[float]


class Searcher:
    def __init__(
        self,
        collection,
        documents: List[str],
        document_ids: List[str],
        metadatas: List[Dict[str, str]],
        bm25,
        tokenized_documents: List[List[str]],
        provider: Optional[OpenRouterClient] = None,
    ):
        self.collection = collection
        self.documents = documents
        self.document_ids = document_ids
        self.metadatas = metadatas
        self.bm25 = bm25
        self.tokenized_documents = tokenized_documents
        self.stemmer = PorterStemmer()
        self.stop_words = DEFAULT_STOP_WORDS
        self.provider = provider or OpenRouterClient.from_env()
        self.metadata_by_id = dict(zip(document_ids, metadatas))
        self.document_by_id = dict(zip(document_ids, documents))

    def cleanup(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
        return False

    def extract_important_terms(self, query: str) -> Tuple[Set[str], Set[str], List[str]]:
        quoted_phrases = re.findall(r'"([^"]*)"', query)
        clean_query = query
        for phrase in quoted_phrases:
            clean_query = clean_query.replace(f'"{phrase}"', "")
        terms = set(clean_query.lower().split())
        stemmed_terms = {self.stemmer.stem(term) for term in terms}
        return terms, stemmed_terms, quoted_phrases

    def min_max_scale(self, arr: pd.Series) -> pd.Series:
        mn = arr.min()
        mx = arr.max()
        if abs(mx - mn) < 1e-12:
            return pd.Series(0.5, index=arr.index, name=arr.name)
        return (arr - mn) / (mx - mn)

    def calculate_keyword_scores(self, query: str) -> pd.Series:
        query_tokens = tokenize_for_bm25(query, self.stop_words, self.stemmer)
        bm25_scores = self.bm25.get_scores(query_tokens)
        _, _, quoted_phrases = self.extract_important_terms(query)

        keyword_scores: Dict[str, float] = {}
        for doc_id, doc, base_score in zip(self.document_ids, self.documents, bm25_scores):
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

    def reciprocal_rank_fusion(
        self,
        semantic_scores: pd.Series,
        keyword_scores: pd.Series,
        allowed_ids: Optional[Collection[str]] = None,
        weight: float = 0.5,
        k: int = 60,
    ) -> pd.DataFrame:
        allowed_ids = set(allowed_ids) if allowed_ids else None
        sem = pd.Series(semantic_scores).copy()
        kw = pd.Series(keyword_scores).copy()
        if allowed_ids:
            sem = sem[sem.index.isin(allowed_ids)]
            kw = kw[kw.index.isin(allowed_ids)]

        sem = sem.dropna()
        kw = kw.dropna()
        sem_rank = sem.rank(method="average", ascending=False)
        kw_rank = kw.rank(method="average", ascending=False)
        df = pd.DataFrame({"sem_rank": sem_rank, "kw_rank": kw_rank})

        sem_comp = (weight / (k + df["sem_rank"])).fillna(0.0)
        kw_comp = ((1.0 - weight) / (k + df["kw_rank"])).fillna(0.0)
        fused = sem_comp + kw_comp
        sem_comp = self.min_max_scale(sem_comp)
        kw_comp = self.min_max_scale(kw_comp)
        fused = self.min_max_scale(fused)
        sem_comp.name = "semantic_score"
        kw_comp.name = "keyword_score"
        fused.name = "fused_score"
        return pd.concat([sem_comp, kw_comp, fused], axis=1).sort_values(
            "fused_score", ascending=False
        )

    def zscore_sigmoid_fusion(
        self,
        semantic_scores: pd.Series,
        keyword_scores: pd.Series,
        allowed_ids: Optional[Collection[str]] = None,
        temperature: float = 1.0,
        weight: float = 0.5,
        eps: float = 1e-8,
    ) -> pd.DataFrame:
        idx = semantic_scores.index.intersection(keyword_scores.index)
        if allowed_ids is not None:
            idx = idx.intersection(pd.Index(allowed_ids))
        if len(idx) == 0:
            return pd.DataFrame(dtype=float)

        inv_temp = 1.0 / max(float(temperature), eps)

        def _normalize(series: pd.Series) -> pd.Series:
            values = series.loc[idx].astype(float)
            mean = float(np.nanmean(values.to_numpy()))
            std = float(np.nanstd(values.to_numpy()))
            denom = std if std > eps else 1.0
            z = (values - mean) / denom
            return pd.Series(1.0 / (1.0 + np.exp(-z * inv_temp)), index=idx)

        sem_norm = _normalize(semantic_scores)
        key_norm = _normalize(keyword_scores)
        fused = (sem_norm * weight + key_norm * (1.0 - weight)).astype(float)
        combined = pd.concat([sem_norm, key_norm, fused], axis=1)
        combined.columns = ["semantic_score", "keyword_score", "fused_score"]
        return combined.sort_values("fused_score", ascending=False)

    def calculate_recency_scores(self, doc_ids: List[str], decay_days: float = 365.0) -> pd.Series:
        if not doc_ids:
            return pd.Series(dtype=float)

        recency_scores = {}
        current_date = datetime.now(timezone.utc)
        for doc_id in doc_ids:
            metadata = self.metadata_by_id.get(doc_id, {})
            raw_date = metadata.get("date")
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

    def hybrid_search(
        self,
        query: str,
        number_of_results: Optional[int] = None,
        semantic_weight: Optional[float] = None,
        must_include_terms: Optional[List[str]] = None,
        top_k: Optional[int] = None,
        combine_strategy: Optional[str] = None,
        rrf_k: Optional[int] = None,
        zsigmoid_temperature: Optional[float] = None,
        recency_boost_enabled: Optional[bool] = None,
        recency_weight: Optional[float] = None,
        recency_decay_days: Optional[float] = None,
    ) -> HybridSearchResult:
        semantic_wt = (
            float(SEARCH_CONFIG.get("semantic_weight", 0.5))
            if semantic_weight is None
            else semantic_weight
        )
        n_results = (
            int(SEARCH_CONFIG.get("n_results", 10))
            if number_of_results is None
            else number_of_results
        )
        candidate_pool_size = (
            int(SEARCH_CONFIG.get("default_top_k", 150))
            if top_k is None
            else top_k
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

        allowed_ids = set(self.document_ids)
        if must_include_terms:
            normalized_terms = [
                normalize_no_punct(term)
                for term in must_include_terms
                if normalize_no_punct(term)
            ]
            allowed_ids = {
                doc_id
                for doc_id, document in zip(self.document_ids, self.documents)
                if all(
                    re.search(rf"(?<!\w){re.escape(term)}(?!\w)", normalize_no_punct(document))
                    for term in normalized_terms
                )
            }

        if not allowed_ids:
            raise ValueError("No documents match the required terms.")

        query_embedding = self.provider.embed_texts([query])[0]
        semantic_results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=min(len(self.document_ids), candidate_pool_size),
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

        keyword_scores = self.calculate_keyword_scores(query)
        top_keyword_scores = keyword_scores.nlargest(min(len(keyword_scores), candidate_pool_size))
        candidate_ids = list(
            (set(semantic_scores.index) | set(top_keyword_scores.index)) & set(allowed_ids)
        )
        if not candidate_ids:
            raise ValueError("No candidate documents available for the query.")

        raw_scores = pd.DataFrame(index=candidate_ids)
        raw_scores["semantic_scores"] = semantic_scores.reindex(candidate_ids).fillna(0.0)
        raw_scores["keyword_scores"] = keyword_scores.reindex(candidate_ids).fillna(0.0)

        if strategy == "rrf":
            fused = self.reciprocal_rank_fusion(
                raw_scores["semantic_scores"],
                raw_scores["keyword_scores"],
                allowed_ids=candidate_ids,
                weight=semantic_wt,
                k=rrf_k_val,
            )
        elif strategy == "zsigmoid":
            fused = self.zscore_sigmoid_fusion(
                raw_scores["semantic_scores"],
                raw_scores["keyword_scores"],
                allowed_ids=candidate_ids,
                temperature=zsig_temp,
                weight=semantic_wt,
            )
        else:
            fused = pd.DataFrame(index=candidate_ids)
            fused["semantic_score"] = self.min_max_scale(raw_scores["semantic_scores"])
            fused["keyword_score"] = self.min_max_scale(raw_scores["keyword_scores"])
            fused["fused_score"] = (
                fused["semantic_score"] * semantic_wt
                + fused["keyword_score"] * (1.0 - semantic_wt)
            )
            fused = fused.sort_values("fused_score", ascending=False)

        rerank_enabled = bool(self.provider.rerank_model)
        rerank_pool_size = min(len(fused), int(SEARCH_CONFIG.get("rerank_top_k", 20)))
        rerank_input = fused.head(rerank_pool_size)
        if rerank_enabled:
            try:
                reranked = self.provider.rerank(
                    query=query,
                    documents=[self.document_by_id[doc_id] for doc_id in rerank_input.index],
                    ids=list(rerank_input.index),
                )
            except OpenRouterError:
                rerank_enabled = False
                reranked = pd.DataFrame({"score": rerank_input["fused_score"]}, index=rerank_input.index)
        else:
            reranked = pd.DataFrame({"score": rerank_input["fused_score"]}, index=rerank_input.index)

        # Preserve the raw rerank score for debugging/inspection.
        fused = fused.join(reranked["score"].rename("reranked_raw_score"), how="left")
        fused["reranked_raw_score"] = fused["reranked_raw_score"].fillna(fused["fused_score"])

        # (c) Treat Cohere rerank output as ranks rather than calibrated scores.
        # Cohere's `relevance_score` is a cross-encoder output squashed to [0, 1]
        # but it is not a probability and is not comparable across queries --
        # using the raw magnitude in a weighted combination with recency causes
        # uncalibrated score gaps to get amplified. Converting to a rank-based
        # score in [0.5, 1.0] preserves the ordering the reranker is good at
        # while discarding the magnitude we don't trust.
        use_ranks = bool(SEARCH_CONFIG.get("rerank_use_ranks", True))
        if use_ranks and rerank_enabled and len(reranked) > 0:
            ordered_ids = list(reranked.sort_values("score", ascending=False).index)
            denom = max(len(ordered_ids) - 1, 1)
            rank_scores = pd.Series(
                {
                    doc_id: 1.0 - (position / denom) * 0.5
                    for position, doc_id in enumerate(ordered_ids)
                },
                name="reranked_score",
                dtype=float,
            )
            fused["reranked_score"] = rank_scores.reindex(fused.index)
            # Docs outside the rerank pool keep their fused_score (already in
            # [0, 1]); since reranked docs get >= 0.5, ordering is preserved
            # for any reasonable n_results <= rerank_top_k.
            fused["reranked_score"] = fused["reranked_score"].fillna(fused["fused_score"])
        else:
            fused["reranked_score"] = fused["reranked_raw_score"]

        # (b) LLM judge: for the top `judge_top_k` docs after rerank, ask a
        # small LLM "does this document actually answer the query?" on a 1-5
        # scale. This is better calibrated to our notion of relevance than
        # the cross-encoder score and filters out topically-overlapping-but-
        # unhelpful notes that Cohere can score highly.
        judge_enabled = (
            bool(SEARCH_CONFIG.get("judge_enabled", True))
            and bool(self.provider.judge_model)
        )
        judge_top_k_val = min(len(fused), int(SEARCH_CONFIG.get("judge_top_k", 10)))
        judge_votes_val = max(1, int(SEARCH_CONFIG.get("judge_votes", 6)))
        fused["judge_raw"] = float("nan")
        fused["judge_score"] = float("nan")
        fused["judge_grade"] = ""
        fused["judge_votes"] = [[] for _ in range(len(fused))]
        fused["judge_reasoning"] = ""
        if judge_enabled and judge_top_k_val > 0:
            post_rerank_order = fused.sort_values("reranked_score", ascending=False)
            judge_candidates = list(post_rerank_order.head(judge_top_k_val).index)
            try:
                judge_df = self.provider.judge_relevance(
                    query=query,
                    documents=[self.document_by_id[doc_id] for doc_id in judge_candidates],
                    ids=judge_candidates,
                    num_votes=judge_votes_val,
                )
                for column in ("judge_raw", "judge_score", "judge_grade", "judge_reasoning"):
                    if column in judge_df.columns:
                        fused.loc[judge_df.index, column] = judge_df[column]
                if "judge_votes" in judge_df.columns:
                    for doc_id, votes in judge_df["judge_votes"].items():
                        fused.at[doc_id, "judge_votes"] = list(votes)
            except OpenRouterError:
                judge_enabled = False

        # When the judge ran, it's the authoritative signal: judged docs should
        # always rank ahead of unjudged candidates, even when rated 1/5. Pushing
        # unjudged docs into negative territory preserves their relative order
        # while guaranteeing any judged doc outranks them.
        if judge_enabled:
            unjudged_mask = fused["judge_score"].isna()
            fused["relevance_score"] = fused["judge_score"]
            fused.loc[unjudged_mask, "relevance_score"] = (
                fused.loc[unjudged_mask, "reranked_score"] - 1.0
            )
        else:
            fused["relevance_score"] = fused["reranked_score"]

        if judge_enabled and bool(SEARCH_CONFIG.get("judge_filter_irrelevant", False)):
            # judge_raw is the average (1-5); drop docs averaging <= 1 (all "not relevant").
            keep = fused["judge_raw"].isna() | (fused["judge_raw"] > 1.0)
            fused = fused[keep]

        if use_recency:
            recency_boost_factor = self.calculate_recency_scores(list(fused.index), decay_days).reindex(
                fused.index
            ).fillna(1.0)
            fused["recency_boost_factor"] = recency_boost_factor
            fused["boosted_score"] = (
                fused["relevance_score"] * (1.0 - recency_wt)
                + fused["relevance_score"] * fused["recency_boost_factor"] * recency_wt
            )
        else:
            fused["recency_boost_factor"] = 1.0
            fused["boosted_score"] = fused["relevance_score"]

        top_results = fused.sort_values("boosted_score", ascending=False).head(n_results)

        judge_raw_values = [
            None if pd.isna(value) else float(value)
            for value in top_results["judge_raw"].tolist()
        ]
        judge_votes_values: List[Optional[List[int]]] = []
        for value in top_results["judge_votes"].tolist():
            if isinstance(value, list) and value:
                judge_votes_values.append([int(v) for v in value])
            else:
                judge_votes_values.append(None)
        judge_grade_values = [
            (value if isinstance(value, str) and value else None)
            for value in top_results["judge_grade"].tolist()
        ]

        result: HybridSearchResult = {
            "ids": list(top_results.index),
            "relevant": [True] * len(top_results),
            "documents": [self.document_by_id[doc_id] for doc_id in top_results.index],
            "metadatas": [self.metadata_by_id[doc_id] for doc_id in top_results.index],
            "semantic_scores": raw_scores.reindex(top_results.index)["semantic_scores"].tolist(),
            "keyword_scores": raw_scores.reindex(top_results.index)["keyword_scores"].tolist(),
            "fused_scores": top_results["fused_score"].tolist(),
            "reranked_scores": top_results["reranked_score"].tolist(),
            "reranked_raw_scores": top_results["reranked_raw_score"].tolist(),
            "judge_scores": top_results["judge_score"].tolist(),
            "judge_raw_scores": judge_raw_values,
            "judge_votes": judge_votes_values,
            "judge_grades": judge_grade_values,
            "judge_reasonings": top_results["judge_reasoning"].tolist(),
            "boosted_scores": top_results["boosted_score"].tolist(),
            "recency_boost_factor": top_results["recency_boost_factor"].tolist(),
            "debug_info": {
                "query": query,
                "combine_strategy": strategy,
                "semantic_weight": semantic_wt,
                "recency_boost_enabled": use_recency,
                "recency_weight": recency_wt,
                "recency_decay_days": decay_days,
                "rrf_k": rrf_k_val if strategy == "rrf" else None,
                "zsigmoid_temperature": zsig_temp if strategy == "zsigmoid" else None,
                "rerank_enabled": rerank_enabled,
                "rerank_use_ranks": bool(use_ranks and rerank_enabled),
                "judge_enabled": judge_enabled,
                "judge_top_k": judge_top_k_val if judge_enabled else 0,
                "judge_votes": judge_votes_val if judge_enabled else 0,
                "judge_model": self.provider.judge_model if judge_enabled else None,
            },
        }
        result[f"{strategy}_semantic_scores"] = top_results["semantic_score"].tolist()
        result[f"{strategy}_keyword_scores"] = top_results["keyword_score"].tolist()
        return result


if TESTING:
    from scripts.database_builder import DatabaseBuilder

    builder = DatabaseBuilder()
    searcher = Searcher(
        builder.collection,
        builder.documents,
        builder.document_ids,
        builder.metadatas,
        builder.bm25,
        builder.tokenized_documents,
        provider=builder.provider,
    )
    out = searcher.hybrid_search("Where did I write about OpenClaw?")
    pp(
        pd.DataFrame(
            {
                key: value
                for key, value in out.items()
                if key
                in [
                    "ids",
                    "boosted_scores",
                    "recency_boost_factor",
                    "fused_scores",
                    "rrf_semantic_scores",
                    "rrf_keyword_scores",
                    "keyword_scores",
                    "semantic_scores",
                ]
            }
        )
    )
