"""Tests for vault_spider.enrich.planner."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from vault_spider.enrich.planner import (
    NEIGHBOR_EXCERPT_CHARS,
    EnrichInput,
    _add_focused_section_excerpts,
    build_prompts,
    gather_neighbors,
    plan,
    postprocess,
)
from vault_spider.index.store import IndexStore


def make_input(body="Meeting with Atlas about Beta today.\n", path="Inbox/raw.md",
               frontmatter=None, title="Raw", source_url=None):
    return EnrichInput(
        body=body,
        title=title,
        path=path,
        existing_frontmatter=frontmatter or {},
        given_title=None,
        intent="interview import",
        source_url=source_url,
    )


NEIGHBORS = [
    {"note_id": "n1", "title": "Atlas", "path": "Research/Atlas.md", "excerpt": "x", "score": 0.9},
    {"note_id": "n2", "title": "Beta", "path": "Research/Beta.md", "excerpt": "y", "score": 0.8},
]


class TestNeighborhoodRetrieval:
    def test_broad_search_disables_recency_and_records_channels(self, monkeypatch):
        calls = []

        def fake_search(_searcher, query, **kwargs):
            calls.append((query, kwargs))
            return SimpleNamespace(
                rows=[
                    {
                        "note_id": "n1",
                        "document": "document",
                        "final": 0.8,
                        "metadata": {"title": "Atlas", "path": "Atlas.md"},
                    }
                ]
            )

        monkeypatch.setattr(
            "vault_spider.retrieval.searcher.Searcher.hybrid_search", fake_search
        )

        class Store:
            def granularity_data(self, granularity):
                assert granularity == "section"
                return [], [], [], None

        neighbors = gather_neighbors(Store(), object(), make_input())

        assert len(calls) == 3
        assert all(call[1]["n_results"] == 10 for call in calls)
        assert all(call[1]["recency_boost_enabled"] is False for call in calls)
        assert neighbors[0]["matched_by"] == ["title", "body", "terms"]

    def test_uses_best_semantic_section_and_caps_excerpt(self):
        long_section = "Best focused section " + ("x" * 1000)

        class Collection:
            def __init__(self):
                self.query_args = None

            def query(self, **kwargs):
                self.query_args = kwargs
                return {
                    "documents": [[long_section, "Other section"]],
                    "metadatas": [[
                        {"note_id": "n1", "heading": "Relevant"},
                        {"note_id": "n2", "heading": "Other"},
                    ]],
                }

        class Store:
            collection = Collection()

            def granularity_data(self, granularity):
                return [], [], [
                    {"note_id": "n1"},
                    {"note_id": "n1"},
                    {"note_id": "n2"},
                    {"note_id": "outside"},
                ], None

        class Provider:
            def embed_texts(self, texts):
                assert texts == ["Raw\nMeeting with Atlas about Beta today.\n"]
                return [[0.1, 0.2]]

        neighbors = [dict(NEIGHBORS[0]), dict(NEIGHBORS[1])]
        result = _add_focused_section_excerpts(
            Store(), Provider(), make_input(), neighbors
        )

        assert result[0]["excerpt"] == long_section[:NEIGHBOR_EXCERPT_CHARS]
        assert result[0]["excerpt_heading"] == "Relevant"
        assert Store.collection.query_args["n_results"] == 3
        assert Store.collection.query_args["where"]["$and"][1] == {
            "note_id": {"$in": ["n1", "n2"]}
        }

    def test_prompt_serializes_neighbor_content_as_json(self):
        neighbors = [
            {
                **NEIGHBORS[0],
                "title": 'Atlas "quoted"',
                "excerpt": "first line\nsecond line",
                "matched_by": ["title", "body"],
                "excerpt_heading": "Details",
            }
        ]

        _, user_prompt = build_prompts(make_input(), neighbors)

        encoded_neighbor = user_prompt.split("NEIGHBORS:\n", 1)[1]
        assert json.loads(encoded_neighbor) == {
            "title": 'Atlas "quoted"',
            "path": "Research/Atlas.md",
            "matched_by": ["title", "body"],
            "excerpt_heading": "Details",
            "excerpt": "first line\nsecond line",
        }


class TestPostprocessSafety:
    def test_full_canned_plan(self):
        parsed = {
            "title": "Interview about Atlas",
            "type": "banana",  # invalid
            "aliases": ["AKA"],
            "inline_links": [
                {"target": "Atlas", "anchor_text": "Atlas", "confidence": 0.95},   # valid inline
                {"target": "Ghost", "anchor_text": "Atlas", "confidence": 0.95},  # nonexistent
                {"target": "Beta", "anchor_text": "Beta", "confidence": 0.7},    # demote
            ],
            "related": [],
            "warnings": [],
        }
        result = postprocess(parsed, make_input(), NEIGHBORS)

        inline_targets = {link["target"] for link in result["link_insertions"]}
        assert inline_targets == {"Atlas"}
        assert result["link_insertions"][0]["occurs_at_line"] == 1
        assert result["link_insertions"][0]["target_path"] == "Research/Atlas.md"

        related_targets = {r["target"] for r in result["related_candidates"]}
        assert related_targets == {"Beta"}  # demoted 0.7

        assert "type" not in result["frontmatter_patch"]  # invalid dropped
        assert result["frontmatter_patch"]["aliases"] == ["AKA"]
        assert any("nonexistent" in w for w in result["warnings"])
        assert any("banana" in w for w in result["warnings"])
        assert result["confidence"] == "medium"  # inline survived but warnings present

    def test_already_linked_dropped_silently(self):
        parsed = {
            "title": "T", "type": None, "aliases": [], "related": [], "warnings": [],
            "inline_links": [{"target": "Atlas", "anchor_text": "Atlas", "confidence": 0.95}],
        }
        body = "Notes already mention [[Atlas]] here.\n"
        result = postprocess(parsed, make_input(body=body), NEIGHBORS)
        assert result["link_insertions"] == []
        assert not any("Atlas" in w for w in result["warnings"])

    def test_existing_type_conflict_dropped(self):
        parsed = {"title": "T", "type": "research", "aliases": [], "inline_links": [],
                  "related": [], "warnings": []}
        result = postprocess(parsed, make_input(frontmatter={"type": "idea"}), NEIGHBORS)
        assert "type" not in result["frontmatter_patch"]
        assert any("already has type=idea" in w for w in result["warnings"])

    def test_source_url_flows_into_patch(self):
        parsed = {"title": "T", "type": None, "aliases": [], "inline_links": [],
                  "related": [], "warnings": []}
        inp = make_input(source_url="https://example.com/a")
        result = postprocess(parsed, inp, NEIGHBORS)
        assert result["frontmatter_patch"]["source_url"] == "https://example.com/a"

    def test_anchor_not_in_body_demotes(self):
        parsed = {
            "title": "T", "type": None, "aliases": [], "related": [], "warnings": [],
            "inline_links": [{"target": "Atlas", "anchor_text": "absent-anchor", "confidence": 0.95}],
        }
        result = postprocess(parsed, make_input(), NEIGHBORS)
        assert result["link_insertions"] == []
        assert {r["target"] for r in result["related_candidates"]} == {"Atlas"}


class TestSuggestedPath:
    def test_stdin_no_consensus_uses_inbox(self):
        parsed = {"title": "New Idea", "type": None, "aliases": [], "inline_links": [],
                  "related": [], "warnings": []}
        inp = make_input(path=None, title="New Idea")
        result = postprocess(parsed, inp, NEIGHBORS)  # 2 neighbors, no 3-way consensus
        assert result["suggested_path"] == "Inbox/New Idea.md"

    def test_folder_consensus(self):
        neighbors = [
            {"note_id": str(i), "title": f"T{i}", "path": f"Research/n{i}.md", "excerpt": "", "score": 1.0 - i * 0.1}
            for i in range(4)
        ] + [{"note_id": "9", "title": "T9", "path": "Other/x.md", "excerpt": "", "score": 0.1}]
        parsed = {"title": "New", "type": None, "aliases": [], "inline_links": [],
                  "related": [], "warnings": []}
        inp = make_input(path=None, title="New")
        result = postprocess(parsed, inp, neighbors)
        assert result["suggested_path"] == "Research/New.md"


class TestUnparseable:
    def test_none_parsed_low_confidence(self):
        result = postprocess(None, make_input(), NEIGHBORS)
        assert result["confidence"] == "low"
        assert result["link_insertions"] == []
        assert result["related_candidates"] == []
        assert result["frontmatter_patch"] == {}
        assert result["warnings"] == ["planner failed to produce a usable plan"]

    def test_non_dict_payload_is_treated_as_unusable(self):
        # A top-level JSON array from the model must not crash postprocess.
        result = postprocess([{"target": "Atlas"}], make_input(), NEIGHBORS)
        assert result["confidence"] == "low"
        assert result["frontmatter_patch"] == {}

    def test_non_dict_link_entries_skipped(self):
        parsed = {
            "title": "T", "type": None, "aliases": "notalist", "related": ["oops"],
            "warnings": "notalist",
            "inline_links": ["bad", {"target": "Atlas", "anchor_text": "Atlas", "confidence": 0.95}],
        }
        result = postprocess(parsed, make_input(), NEIGHBORS)
        assert {link["target"] for link in result["link_insertions"]} == {"Atlas"}
        assert "aliases" not in result["frontmatter_patch"]

    def test_invalid_confidences_are_dropped_without_crashing(self):
        parsed = {
            "title": "T",
            "inline_links": [
                {"target": "Atlas", "anchor_text": "Atlas", "confidence": "high"}
            ],
            "related": [{"target": "Beta", "confidence": float("nan")}],
        }

        result = postprocess(parsed, make_input(), NEIGHBORS)

        assert result["link_insertions"] == []
        assert result["related_candidates"] == []
        assert sum("invalid confidence" in warning for warning in result["warnings"]) == 2

    def test_unsafe_title_is_dropped(self):
        parsed = {
            "title": "../../Outside",
            "inline_links": [],
            "related": [],
        }

        result = postprocess(parsed, make_input(), NEIGHBORS)

        assert result["title"] == "Raw"
        assert result["suggested_path"] == "Inbox/raw.md"
        assert any("unsafe path" in warning for warning in result["warnings"])


class TestMutationGuard:
    def test_plan_writes_nothing(self, tmp_path, tiny_vault, fake_provider):
        store = IndexStore(
            chroma_db_path=str(tmp_path / "chroma"),
            collection_name="vault_notes",
            provider=fake_provider,
        )
        store.sync(str(tiny_vault))
        fake_provider.chat_response = json.dumps(
            {"title": "Alpha", "type": "research", "aliases": [], "inline_links": [],
             "related": [], "warnings": []}
        )

        def checksum():
            h = hashlib.sha256()
            for p in sorted(tiny_vault.rglob("*.md")):
                h.update(p.read_bytes())
            return h.hexdigest()

        before = checksum()
        result = plan(make_input(body="text about alpha", path=None), store, fake_provider)
        assert checksum() == before
        assert "confidence" in result
        assert "frontmatter_patch" in result
