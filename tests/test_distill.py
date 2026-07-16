"""Tests for vault_spider.compounding.distill (synthesize --save)."""

from __future__ import annotations

import re

import pytest

from vault_spider.compounding import distill
from vault_spider.corpus.frontmatter import split_frontmatter

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def synth(**overrides):
    base = {
        "question": "What do I know about alpha decay?",
        "answer": "Alpha decay emits helium nuclei.",
        "confidence": "high",
        "abstained": False,
        "citations": [
            {"key": "S0", "note_id": "n1", "path": "Physics/Alpha.md", "title": "Alpha",
             "heading": "Decay", "excerpt": "Alpha particles are helium-4 nuclei emitted..."},
            {"key": "S1", "note_id": "n1", "path": "Physics/Alpha.md", "title": "Alpha",
             "heading": "Other", "excerpt": "second section"},
            {"key": "S2", "note_id": "n2", "path": "Physics/Beta.md", "title": "Beta",
             "heading": "", "excerpt": "beta excerpt"},
        ],
        "notes_used": ["Physics/Alpha.md", "Physics/Beta.md"],
        "warnings": [],
    }
    base.update(overrides)
    return base


class TestSlugify:
    def test_basic(self):
        assert distill.slugify("What do I know about X?") == "what-do-i-know-about-x"

    def test_truncates_to_80(self):
        assert len(distill.slugify("a " * 100)) <= 80

    def test_punctuation_only_is_empty(self):
        assert distill.slugify("?!.,") == ""


class TestSave:
    def test_writes_valid_note(self, tmp_path):
        result = distill.save_distilled_note(synth(), str(tmp_path))
        assert result["saved"] is True
        assert result["saved_path"] == "Distilled/what-do-i-know-about-alpha-decay.md"

        text = (tmp_path / "Distilled" / "what-do-i-know-about-alpha-decay.md").read_text()
        fm, body = split_frontmatter(text)
        assert ULID_RE.match(str(fm["id"]))
        assert fm["type"] == "distilled"
        assert fm["created"] == fm["updated"]
        assert body.startswith("# What do I know about alpha decay?")
        # Dedupe by note_id: two sources (n1, n2), not three citations.
        assert body.count("- [[") == 2
        assert "- [[Alpha]] — Decay: Alpha particles" in body
        assert "- [[Beta]]" in body

    def test_abstained_skips(self, tmp_path):
        result = distill.save_distilled_note(synth(abstained=True), str(tmp_path))
        assert result["saved"] is False
        assert result["warnings"] == ["not saved: model abstained"]

    def test_low_confidence_skips(self, tmp_path):
        result = distill.save_distilled_note(synth(confidence="low"), str(tmp_path))
        assert result["saved"] is False
        assert result["warnings"] == ["not saved: low confidence"]

    def test_no_citations_skips(self, tmp_path):
        result = distill.save_distilled_note(synth(citations=[]), str(tmp_path))
        assert result["saved"] is False
        assert result["warnings"] == ["not saved: no citations"]

    def test_empty_answer_skips(self, tmp_path):
        result = distill.save_distilled_note(synth(answer=""), str(tmp_path))
        assert result["saved"] is False
        assert result["warnings"] == ["not saved: empty answer"]

    def test_existing_file_skips(self, tmp_path):
        distill.save_distilled_note(synth(), str(tmp_path))
        result = distill.save_distilled_note(synth(), str(tmp_path))
        assert result["saved"] is False
        assert "already exists" in result["warnings"][0]

    def test_empty_slug_raises(self, tmp_path):
        with pytest.raises(distill.EmptySlugError):
            distill.save_distilled_note(synth(question="?!."), str(tmp_path))

    def test_save_directory_cannot_escape_vault(self, tmp_path):
        with pytest.raises(distill.InvalidSaveDirectoryError, match="must not contain"):
            distill.save_distilled_note(synth(), str(tmp_path), "../Outside")

        assert not (tmp_path.parent / "Outside").exists()

    def test_duplicate_title_uses_path_target(self, tmp_path):
        citations = [
            {"note_id": "n1", "path": "A/Same.md", "title": "Same", "heading": "H", "excerpt": "x"},
            {"note_id": "n2", "path": "B/Same.md", "title": "Same", "heading": "H", "excerpt": "y"},
        ]
        distill.save_distilled_note(synth(citations=citations), str(tmp_path))
        text = (tmp_path / "Distilled" / "what-do-i-know-about-alpha-decay.md").read_text()
        assert "[[A/Same]]" in text
        assert "[[B/Same]]" in text
