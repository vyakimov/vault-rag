"""Tests for vault_spider.synthesis.answer."""

from __future__ import annotations

import json

from vault_spider.synthesis.answer import parse_llm_json, synthesize


def retrieval_output():
    def candidate(note_id, path, title):
        return {
            "note_id": note_id,
            "path": path,
            "title": title,
            "type": "",
            "heading": "H",
            "chunk_id": f"{note_id}::s000",
            "line_start": 1,
            "line_end": 2,
            "excerpt": f"excerpt for {title}",
            "scores": {"bm25": 1.0, "semantic": 1.0, "fused": 0.5, "reranker": None, "final": 0.5},
            "why": "combined keyword+semantic signal",
        }

    return {
        "query": "what about alpha?",
        "mode": "fast",
        "granularity": "section",
        "candidates": [
            candidate("n1", "a.md", "Alpha"),
            candidate("n2", "b.md", "Beta"),
        ],
    }


class TestParseLlmJson:
    def test_plain_json(self):
        assert parse_llm_json('{"answer": "hi"}') == {"answer": "hi"}

    def test_strips_code_fence(self):
        assert parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_repairs_truncated_json(self):
        truncated = '{"answer": "a long answer that got cut o'
        repaired = parse_llm_json(truncated)
        assert repaired is not None
        assert "answer" in repaired

    def test_unparseable_returns_none(self):
        assert parse_llm_json("no json here at all") is None

    def test_top_level_array_is_not_a_valid_response(self):
        assert parse_llm_json('[{"answer": "hi"}]') is None


class TestSynthesize:
    def test_citation_resolution_and_unknown_key_warning(self, fake_provider):
        fake_provider.chat_response = json.dumps(
            {
                "answer": "Alpha is described in [S0]. Also [S9].",
                "citations": ["S0", "S9"],
                "confidence": "High",
                "abstained": False,
            }
        )
        result = synthesize(fake_provider, retrieval_output())
        assert result["confidence"] == "high"
        assert result["abstained"] is False
        assert [c["key"] for c in result["citations"]] == ["S0"]
        assert result["citations"][0]["path"] == "a.md"
        assert result["notes_used"] == ["a.md"]
        assert result["warnings"] == ["model cited unknown key S9"]

    def test_duplicate_citation_keys_deduped(self, fake_provider):
        fake_provider.chat_response = json.dumps(
            {
                "answer": "Alpha [S0][S0].",
                "citations": ["S0", "S0", "S1"],
                "confidence": "High",
                "abstained": False,
            }
        )
        result = synthesize(fake_provider, retrieval_output())
        assert [c["key"] for c in result["citations"]] == ["S0", "S1"]
        assert result["warnings"] == []

    def test_abstention_propagates(self, fake_provider):
        fake_provider.chat_response = json.dumps(
            {"answer": "", "citations": [], "confidence": "Low", "abstained": True}
        )
        result = synthesize(fake_provider, retrieval_output())
        assert result["abstained"] is True
        assert result["confidence"] == "low"
        assert result["citations"] == []

    def test_unparseable_output_abstains(self, fake_provider):
        fake_provider.chat_response = "the model rambled without any json"
        result = synthesize(fake_provider, retrieval_output())
        assert result["abstained"] is True
        assert result["answer"] == ""
        assert result["warnings"] == ["unparseable model output"]
        assert result["raw"] == "the model rambled without any json"

    def test_long_sentences_with_citations_have_no_warning(self, fake_provider):
        fake_provider.chat_response = json.dumps(
            {
                "answer": "This sufficiently long factual sentence is grounded in the source [S0].",
                "citations": ["S0"],
                "confidence": "High",
                "abstained": False,
            }
        )
        assert synthesize(fake_provider, retrieval_output())["warnings"] == []

    def test_long_uncited_sentences_are_counted(self, fake_provider):
        fake_provider.chat_response = json.dumps(
            {
                "answer": (
                    "This first factual sentence is deliberately longer than forty characters. "
                    "This second factual sentence is also deliberately longer than forty characters."
                ),
                "citations": [],
                "confidence": "Medium",
                "abstained": False,
            }
        )
        assert synthesize(fake_provider, retrieval_output())["warnings"] == [
            "2 sentence(s) lack citations"
        ]

    def test_abstained_answer_skips_coverage_warning(self, fake_provider):
        fake_provider.chat_response = json.dumps(
            {
                "answer": "A deliberately long uncited sentence that would otherwise be flagged.",
                "citations": [],
                "confidence": "Low",
                "abstained": True,
            }
        )
        assert synthesize(fake_provider, retrieval_output())["warnings"] == []

    def test_empty_answer_fails_closed(self, fake_provider):
        fake_provider.chat_response = json.dumps(
            {"answer": "", "citations": [], "confidence": "High", "abstained": False}
        )

        result = synthesize(fake_provider, retrieval_output())

        assert result["abstained"] is True
        assert result["warnings"] == ["model returned an empty answer without abstaining"]

    def test_string_boolean_fails_closed(self, fake_provider):
        fake_provider.chat_response = json.dumps(
            {
                "answer": "This must not be treated as grounded [S0].",
                "citations": ["S0"],
                "confidence": "High",
                "abstained": "false",
            }
        )

        result = synthesize(fake_provider, retrieval_output())

        assert result["abstained"] is True
        assert "model abstained value was not a boolean" in result["warnings"]

    def test_non_array_citations_are_not_iterated_as_text(self, fake_provider):
        fake_provider.chat_response = json.dumps(
            {
                "answer": "Alpha is described here.",
                "citations": "S0",
                "confidence": "High",
                "abstained": False,
            }
        )

        result = synthesize(fake_provider, retrieval_output())

        assert result["citations"] == []
        assert "model citations were not an array" in result["warnings"]
