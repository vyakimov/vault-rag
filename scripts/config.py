"""Configuration for Vault RAG retrieval and ranking."""

BM25_CONFIG = {
    "k1": 1.2,
    "b": 0.75,
}

SEARCH_CONFIG = {
    "semantic_weight": 0.5,
    "default_top_k": 150,
    "n_results": 10,
    "combine_strategy": "rrf",
    "rrf_k": 60,
    "zsigmoid_temperature": 1.0,
    "rerank_top_k": 20,
    # (c) Treat Cohere rerank scores as ranks, not meaningful probabilities,
    # before combining with recency. Keeps ordering, discards the uncalibrated
    # magnitude so recency can't amplify score gaps that don't mean much.
    "rerank_use_ranks": True,
    # (b) Add an LLM "is this relevant?" judge after the cross-encoder rerank.
    # Judges the top `judge_top_k` docs with a 1-5 scale and uses that score
    # in place of the rerank-derived score for downstream ranking.
    "judge_enabled": True,
    "judge_top_k": 10,
    # If True, docs the judge rates 1/5 (not relevant) are dropped from results.
    "judge_filter_irrelevant": False,
    "recency_boost_enabled": True,
    "recency_weight": 0.2,
    "recency_decay_days": 365.0,
}
