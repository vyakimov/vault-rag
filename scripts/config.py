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
    "recency_boost_enabled": True,
    "recency_weight": 0.2,
    "recency_decay_days": 365.0,
}
