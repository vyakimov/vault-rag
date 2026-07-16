"""Score-fusion functions for hybrid retrieval (pure, no ranker state)."""

from __future__ import annotations

from typing import Collection, Optional

import numpy as np
import pandas as pd


def min_max_scale(arr: pd.Series) -> pd.Series:
    mn = arr.min()
    mx = arr.max()
    if abs(mx - mn) < 1e-12:
        return pd.Series(0.5, index=arr.index, name=arr.name)
    return (arr - mn) / (mx - mn)


def reciprocal_rank_fusion(
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
    sem_comp = min_max_scale(sem_comp)
    kw_comp = min_max_scale(kw_comp)
    fused = min_max_scale(fused)
    sem_comp.name = "semantic_score"
    kw_comp.name = "keyword_score"
    fused.name = "fused_score"
    # kind="stable" keeps tied scores in input-index order; the default
    # quicksort makes tie order vary with the numpy version.
    return pd.concat([sem_comp, kw_comp, fused], axis=1).sort_values(
        "fused_score", ascending=False, kind="stable"
    )


def zscore_sigmoid_fusion(
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
    return combined.sort_values("fused_score", ascending=False, kind="stable")
