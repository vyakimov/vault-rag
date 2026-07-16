import json

from vault_spider.index.store import IndexStore
from vault_spider.retrieval.query_cache import QueryEmbeddingCache
from vault_spider.retrieval.searcher import Searcher


def test_put_get_roundtrip(tmp_path):
    cache = QueryEmbeddingCache(str(tmp_path / "cache.json"), "model-a")
    cache.put("query", [1.0, 2.0])
    assert cache.get("query") == [1.0, 2.0]


def test_model_mismatch_invalidates(tmp_path):
    path = str(tmp_path / "cache.json")
    QueryEmbeddingCache(path, "model-a").put("query", [1.0])
    assert QueryEmbeddingCache(path, "model-b").get("query") is None


def test_corrupt_file_is_tolerated(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("not json", encoding="utf-8")
    cache = QueryEmbeddingCache(str(path), "model")
    assert cache.get("query") is None
    cache.put("query", [3.0])
    assert cache.get("query") == [3.0]


def test_eviction_keeps_newest_entries(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    cache = QueryEmbeddingCache(str(path), "model", max_entries=3)
    timestamps = iter(range(8))
    monkeypatch.setattr("vault_spider.retrieval.query_cache.time.time", lambda: next(timestamps))
    for index in range(8):
        cache.put(f"q{index}", [float(index)])

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["entries"]) == 3
    assert [cache.get(f"q{index}") for index in range(5)] == [None] * 5
    assert [cache.get(f"q{index}") for index in range(5, 8)] == [
        [5.0],
        [6.0],
        [7.0],
    ]


def test_searcher_reuses_query_embedding(tmp_path, tiny_vault, fake_provider):
    store = IndexStore(str(tmp_path / "chroma"), provider=fake_provider)
    store.sync(str(tiny_vault))
    fake_provider.embed_calls.clear()
    searcher = Searcher(store, provider=fake_provider)

    first = searcher.hybrid_search("repeat query")
    first_calls = list(fake_provider.embed_calls)
    fake_provider.embed_calls.clear()
    second = searcher.hybrid_search("repeat query")

    assert len(first_calls) == 1
    assert fake_provider.embed_calls == []
    assert first.rows == second.rows
    assert first.debug_info["query_cache"] == "miss"
    assert second.debug_info["query_cache"] == "hit"
