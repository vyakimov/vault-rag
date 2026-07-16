from dataclasses import fields

import pytest

from vault_spider.config import DEFAULT_SEARCH_PARAMS, SEARCH_CONFIG, SearchParams


def test_with_overrides_ignores_none_and_applies_values():
    params = SearchParams().with_overrides(n_results=4, semantic_weight=None)

    assert params.n_results == 4
    assert params.semantic_weight == 0.5


def test_default_search_params_match_legacy_config():
    for field in fields(SearchParams):
        config_key = "default_top_k" if field.name == "top_k" else field.name
        if config_key in SEARCH_CONFIG:
            assert getattr(DEFAULT_SEARCH_PARAMS, field.name) == SEARCH_CONFIG[config_key]


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"n_results": 0}, "n_results"),
        ({"top_k": 0}, "top_k"),
        ({"semantic_weight": 1.1}, "semantic_weight"),
        ({"combine_strategy": "mystery"}, "combine_strategy"),
        ({"recency_decay_days": 0}, "recency_decay_days"),
        ({"zsigmoid_temperature": float("nan")}, "zsigmoid_temperature"),
    ],
)
def test_invalid_search_parameters_are_rejected(override, message):
    with pytest.raises(ValueError, match=message):
        SearchParams(**override)
