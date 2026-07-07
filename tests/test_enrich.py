"""Tests for vault_rag.enrich.planner."""

from __future__ import annotations

import hashlib
import json

from vault_rag.enrich.planner import EnrichInput, plan, postprocess
from vault_rag.index.store import IndexStore


def make_input(body="Meeting with Rose about Beta today.\n", path="Inbox/raw.md",
               frontmatter=None, title="Raw", source_type=None, source_url=None):
    return EnrichInput(
        body=body,
        title=title,
        path=path,
        existing_frontmatter=frontmatter or {},
        given_title=None,
        intent="interview import",
        source_type=source_type,
        source_url=source_url,
    )


NEIGHBORS = [
    {"note_id": "n1", "title": "Rose", "path": "Research/Rose.md", "excerpt": "x", "score": 0.9},
    {"note_id": "n2", "title": "Beta", "path": "Research/Beta.md", "excerpt": "y", "score": 0.8},
]


class TestPostprocessSafety:
    def test_full_canned_plan(self):
        parsed = {
            "title": "Interview about Rose",
            "type": "banana",  # invalid
            "aliases": ["AKA"],
            "inline_links": [
                {"target": "Rose", "anchor_text": "Rose", "confidence": 0.95},   # valid inline
                {"target": "Ghost", "anchor_text": "Rose", "confidence": 0.95},  # nonexistent
                {"target": "Beta", "anchor_text": "Beta", "confidence": 0.7},    # demote
            ],
            "related": [],
            "warnings": [],
        }
        result = postprocess(parsed, make_input(), NEIGHBORS)

        inline_targets = {l["target"] for l in result["link_insertions"]}
        assert inline_targets == {"Rose"}
        assert result["link_insertions"][0]["occurs_at_line"] == 1
        assert result["link_insertions"][0]["target_path"] == "Research/Rose.md"

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
            "inline_links": [{"target": "Rose", "anchor_text": "Rose", "confidence": 0.95}],
        }
        body = "Notes already mention [[Rose]] here.\n"
        result = postprocess(parsed, make_input(body=body), NEIGHBORS)
        assert result["link_insertions"] == []
        assert not any("Rose" in w for w in result["warnings"])

    def test_existing_type_conflict_dropped(self):
        parsed = {"title": "T", "type": "research", "aliases": [], "inline_links": [],
                  "related": [], "warnings": []}
        result = postprocess(parsed, make_input(frontmatter={"type": "idea"}), NEIGHBORS)
        assert "type" not in result["frontmatter_patch"]
        assert any("already has type=idea" in w for w in result["warnings"])

    def test_source_type_and_url_flow_into_patch(self):
        parsed = {"title": "T", "type": None, "aliases": [], "inline_links": [],
                  "related": [], "warnings": []}
        inp = make_input(source_type="web", source_url="https://example.com/a")
        result = postprocess(parsed, inp, NEIGHBORS)
        assert result["frontmatter_patch"]["source_type"] == "web"
        assert result["frontmatter_patch"]["source_url"] == "https://example.com/a"

    def test_anchor_not_in_body_demotes(self):
        parsed = {
            "title": "T", "type": None, "aliases": [], "related": [], "warnings": [],
            "inline_links": [{"target": "Rose", "anchor_text": "absent-anchor", "confidence": 0.95}],
        }
        result = postprocess(parsed, make_input(), NEIGHBORS)
        assert result["link_insertions"] == []
        assert {r["target"] for r in result["related_candidates"]} == {"Rose"}


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
        result = postprocess([{"target": "Rose"}], make_input(), NEIGHBORS)
        assert result["confidence"] == "low"
        assert result["frontmatter_patch"] == {}

    def test_non_dict_link_entries_skipped(self):
        parsed = {
            "title": "T", "type": None, "aliases": "notalist", "related": ["oops"],
            "warnings": "notalist",
            "inline_links": ["bad", {"target": "Rose", "anchor_text": "Rose", "confidence": 0.95}],
        }
        result = postprocess(parsed, make_input(), NEIGHBORS)
        assert {l["target"] for l in result["link_insertions"]} == {"Rose"}
        assert "aliases" not in result["frontmatter_patch"]


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
