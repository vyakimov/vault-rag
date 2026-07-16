"""Tests for vault_spider.retrieval.evidence and the mixed-mode per-note cap."""

from __future__ import annotations

from collections import Counter

from vault_spider.index.store import IndexStore
from vault_spider.retrieval.evidence import build_retrieval_output
from vault_spider.retrieval.searcher import Searcher


def make_row(note_id, chunk_id, *, bm25, semantic, reranker=None, rerank_rank=None,
             granularity="section", heading="Sec", final=0.5):
    return {
        "id": chunk_id,
        "note_id": note_id,
        "document": "the entry text " * 100,
        "metadata": {
            "note_id": note_id,
            "granularity": granularity,
            "title": "Title",
            "path": f"{note_id}.md",
            "folder": ".",
            "note_type": "",
            "heading": heading,
            "line_start": 3,
            "line_end": 9,
        },
        "bm25": bm25,
        "semantic": semantic,
        "fused": 0.6,
        "reranker": reranker,
        "final": final,
        "rerank_rank": rerank_rank,
    }


class TestSchema:
    def test_candidate_has_all_keys(self):
        rows = [make_row("n1", "n1::s000", bm25=1.0, semantic=2.0)]
        out = build_retrieval_output("q", "fast", "section", rows)
        assert out["query"] == "q"
        assert out["mode"] == "fast"
        assert out["granularity"] == "section"
        cand = out["candidates"][0]
        for key in (
            "note_id", "path", "title", "type", "heading", "chunk_id",
            "line_start", "line_end", "excerpt", "scores", "why",
        ):
            assert key in cand
        for key in ("bm25", "semantic", "fused", "reranker", "final"):
            assert key in cand["scores"]
        assert len(cand["excerpt"]) <= 700

    def test_document_candidate_reports_zero_lines_and_blank_heading(self):
        rows = [make_row("n1", "n1::doc", bm25=1.0, semantic=1.0,
                         granularity="document", heading="")]
        rows[0]["metadata"]["line_start"] = 0
        rows[0]["metadata"]["line_end"] = 0
        cand = build_retrieval_output("q", "fast", "document", rows)["candidates"][0]
        assert cand["heading"] == ""
        assert cand["line_start"] == 0
        assert cand["line_end"] == 0


class TestFastModeRerankerNull:
    def test_reranker_is_null(self):
        rows = [make_row("n1", "n1::s000", bm25=1.0, semantic=2.0, reranker=None)]
        cand = build_retrieval_output("q", "fast", "section", rows)["candidates"][0]
        assert cand["scores"]["reranker"] is None


class TestWhyRules:
    def test_reranked_top_wins(self):
        rows = [make_row("n1", "n1::s000", bm25=0.0, semantic=5.0,
                         reranker=0.9, rerank_rank=2)]
        cand = build_retrieval_output("q", "thorough", "section", rows)["candidates"][0]
        assert cand["why"] == "reranked into top 2 for this query"

    def test_keyword_vs_semantic(self):
        rows = [
            make_row("n1", "n1::s000", bm25=10.0, semantic=0.0),
            make_row("n2", "n2::s000", bm25=0.0, semantic=10.0),
        ]
        cands = build_retrieval_output("q", "fast", "section", rows)["candidates"]
        assert cands[0]["why"] == "strong keyword match"
        assert cands[1]["why"] == "strong semantic match"

    def test_combined_when_equal(self):
        rows = [
            make_row("n1", "n1::s000", bm25=1.0, semantic=1.0),
            make_row("n2", "n2::s000", bm25=1.0, semantic=1.0),
        ]
        cands = build_retrieval_output("q", "fast", "section", rows)["candidates"]
        assert all(c["why"] == "combined keyword+semantic signal" for c in cands)


class TestMixedPerNoteCap:
    def test_at_most_three_sections_per_note(self, tmp_path, tiny_vault, fake_provider):
        store = IndexStore(
            chroma_db_path=str(tmp_path / "chroma"),
            collection_name="vault_notes",
            provider=fake_provider,
        )
        store.sync(str(tiny_vault))
        searcher = Searcher(store, granularity="mixed", provider=fake_provider)
        result = searcher.hybrid_search(
            "zqxq marker filler", mode="fast", granularity="mixed", n_results=20
        )
        per_note = Counter(row["note_id"] for row in result.rows)
        assert per_note  # retrieved something
        assert max(per_note.values()) <= 3
