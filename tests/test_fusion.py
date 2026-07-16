"""Golden-value tests for vault_spider.retrieval.fusion.

Expected values were computed with the pre-refactor Searcher implementation
(scripts/searcher.py at git HEAD) for the fixed input below and hard-coded here
so any change to the fusion math is caught.
"""

from __future__ import annotations

import pandas as pd
import pytest

from vault_spider.retrieval.fusion import (
    min_max_scale,
    reciprocal_rank_fusion,
    zscore_sigmoid_fusion,
)

SEM = pd.Series({"a": 1.9, "b": 1.5, "c": 1.2, "d": 1.8, "e": 1.1}, name="semantic_scores")
KW = pd.Series({"a": 0.0, "b": 3.2, "c": 5.0, "d": 1.0, "e": 0.5}, name="keyword_scores")
IDS = ["a", "b", "c", "d", "e"]


class TestMinMaxScale:
    def test_scales_to_unit_range(self):
        scaled = min_max_scale(pd.Series([0.0, 5.0, 10.0]))
        assert list(scaled) == [0.0, 0.5, 1.0]

    def test_constant_series_maps_to_half(self):
        scaled = min_max_scale(pd.Series([2.0, 2.0, 2.0]))
        assert list(scaled) == [0.5, 0.5, 0.5]


class TestReciprocalRankFusion:
    def test_matches_golden_values(self):
        fused = reciprocal_rank_fusion(SEM, KW, allowed_ids=IDS, weight=0.5, k=60)
        assert list(fused.index) == ["c", "b", "d", "a", "e"]
        expected = {"c": 1.0, "b": 0.983749, "d": 0.983749, "a": 0.761719, "e": 0.0}
        for doc_id, value in expected.items():
            assert fused.loc[doc_id, "fused_score"] == pytest.approx(value, abs=1e-6)


class TestZScoreSigmoidFusion:
    def test_matches_golden_values(self):
        fused = zscore_sigmoid_fusion(SEM, KW, allowed_ids=IDS, temperature=1.0, weight=0.5)
        assert list(fused.index) == ["b", "c", "d", "a", "e"]
        expected = {
            "b": 0.580775,
            "c": 0.557499,
            "d": 0.549195,
            "a": 0.521292,
            "e": 0.268742,
        }
        for doc_id, value in expected.items():
            assert fused.loc[doc_id, "fused_score"] == pytest.approx(value, abs=1e-6)
