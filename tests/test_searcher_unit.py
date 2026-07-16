import pandas as pd

from vault_spider.index.store import IndexStore
from vault_spider.retrieval.searcher import Searcher
from vault_spider.utils import tokenize_for_bm25


def test_unquoted_keyword_scores_match_bm25(tmp_path, tiny_vault, fake_provider):
    store = IndexStore(str(tmp_path / "chroma"), provider=fake_provider)
    store.sync(str(tiny_vault))
    documents, ids, _, bm25 = store.granularity_data("document")
    searcher = Searcher(store, provider=fake_provider)

    actual = searcher.calculate_keyword_scores("alpha notes", ids, documents, bm25)
    tokens = tokenize_for_bm25("alpha notes", searcher.stop_words, searcher.stemmer)
    expected = pd.Series(dict(zip(ids, bm25.get_scores(tokens))), dtype=float)

    pd.testing.assert_series_equal(actual, expected, check_names=False)


def test_quoted_phrase_applies_boost(tmp_path, tiny_vault, fake_provider):
    store = IndexStore(str(tmp_path / "chroma"), provider=fake_provider)
    store.sync(str(tiny_vault))
    documents, ids, _, bm25 = store.granularity_data("document")
    searcher = Searcher(store, provider=fake_provider)

    actual = searcher.calculate_keyword_scores('"zqxq"', ids, documents, bm25)
    tokens = tokenize_for_bm25('"zqxq"', searcher.stop_words, searcher.stemmer)
    base = dict(zip(ids, bm25.get_scores(tokens)))
    big_id = next(doc_id for doc_id, document in zip(ids, documents) if "zqxq" in document)

    assert actual[big_id] == float(base[big_id]) * 1.3
