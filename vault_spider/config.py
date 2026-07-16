"""Configuration for Vault Spider retrieval and ranking."""

import math
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class SearchParams:
    semantic_weight: float = 0.5
    top_k: int = 150
    n_results: int = 10
    combine_strategy: str = "rrf"
    rrf_k: int = 60
    zsigmoid_temperature: float = 1.0
    rerank_top_k: int = 30
    rerank_use_ranks: bool = True
    recency_boost_enabled: bool = True
    recency_weight: float = 0.2
    recency_decay_days: float = 365.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.semantic_weight <= 1.0:
            raise ValueError("semantic_weight must be between 0 and 1")
        if self.top_k < 1:
            raise ValueError("top_k must be at least 1")
        if self.n_results < 1:
            raise ValueError("n_results must be at least 1")
        if self.combine_strategy not in {"rrf", "zsigmoid", "minmax"}:
            raise ValueError("combine_strategy must be rrf, zsigmoid, or minmax")
        if self.rrf_k < 1:
            raise ValueError("rrf_k must be at least 1")
        if not math.isfinite(self.zsigmoid_temperature) or self.zsigmoid_temperature <= 0:
            raise ValueError("zsigmoid_temperature must be greater than 0")
        if self.rerank_top_k < 1:
            raise ValueError("rerank_top_k must be at least 1")
        if not 0.0 <= self.recency_weight <= 1.0:
            raise ValueError("recency_weight must be between 0 and 1")
        if not math.isfinite(self.recency_decay_days) or self.recency_decay_days <= 0:
            raise ValueError("recency_decay_days must be greater than 0")

    def with_overrides(self, **overrides) -> "SearchParams":
        provided = {key: value for key, value in overrides.items() if value is not None}
        return replace(self, **provided)


DEFAULT_SEARCH_PARAMS = SearchParams()

BM25_CONFIG = {
    "k1": 1.2,
    "b": 0.75,
}

# Deprecated: read DEFAULT_SEARCH_PARAMS. Kept only until external readers migrate.
SEARCH_CONFIG = {
    "semantic_weight": 0.5,
    "default_top_k": 150,
    "n_results": 10,
    "combine_strategy": "rrf",
    "rrf_k": 60,
    "zsigmoid_temperature": 1.0,
    "rerank_top_k": 30,
    # (c) Treat Cohere rerank scores as ranks, not meaningful probabilities,
    # before combining with recency. Keeps ordering, discards the uncalibrated
    # magnitude so recency can't amplify score gaps that don't mean much.
    "rerank_use_ranks": True,
    "recency_boost_enabled": True,
    "recency_weight": 0.2,
    "recency_decay_days": 365.0,
}
