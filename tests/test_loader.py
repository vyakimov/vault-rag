"""Tests for vault_rag.corpus.loader (Note loading, date resolution, doc text)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from vault_rag.corpus.chunker import document_text
from vault_rag.corpus.loader import (
    Note,
    has_ignore_tag,
    load_notes,
    resolve_note_date,
)

FIXTURES = Path(__file__).parent / "fixtures" / "notes"


class TestHasIgnoreTag:
    def test_detects_ignore_tag(self):
        assert has_ignore_tag("some text #ignore more text")

    def test_detects_secret_tag(self):
        assert has_ignore_tag("#secret stuff")

    def test_case_insensitive(self):
        assert has_ignore_tag("#Ignore")

    def test_plain_word_not_matched(self):
        assert not has_ignore_tag("please ignore this")

    def test_subtag_not_matched(self):
        assert not has_ignore_tag("#ignored-for-now")


class TestResolveNoteDate:
    def test_frontmatter_date_wins(self, tmp_path: Path):
        path = tmp_path / "whatever.md"
        path.write_text("body")
        assert resolve_note_date(path, {"date": "2024-05-01"}).startswith("2024-05-01")

    def test_falls_back_to_filename(self, tmp_path: Path):
        path = tmp_path / "2023-09-15.md"
        path.write_text("body")
        assert resolve_note_date(path, {}).startswith("2023-09-15")

    def test_falls_back_to_mtime(self, tmp_path: Path):
        path = tmp_path / "untitled.md"
        path.write_text("body")
        parsed = dt.datetime.fromisoformat(resolve_note_date(path, {}))
        assert parsed.tzinfo is not None


class TestDocumentText:
    def _note(self, **kw) -> Note:
        base = dict(
            note_id="x",
            path="folder/note.md",
            title="Title",
            tags=["a", "b"],
            created=None,
            updated=None,
            date="2024-01-01T00:00:00+00:00",
            note_type="",
            body="hello",
            raw_text="hello",
            content_hash="h",
        )
        base.update(kw)
        return Note(**base)

    def test_includes_title_path_tags_date_body(self):
        text = document_text(self._note())
        assert "# Title" in text
        assert "Path: folder/note.md" in text
        assert "Tags: a, b" in text
        assert "Date: 2024-01-01" in text
        assert "hello" in text

    def test_omits_tags_when_empty(self):
        text = document_text(self._note(tags=[], date="", body="body"))
        assert "Tags:" not in text


class TestLoadNotes:
    def test_loads_all_fixture_notes(self):
        notes = load_notes(str(FIXTURES))
        assert len(notes) == 20

    def test_each_note_has_required_metadata(self):
        for note in load_notes(str(FIXTURES)):
            assert note.title
            assert note.path.endswith(".md")
            assert note.content_hash
            dt.datetime.fromisoformat(note.date)

    def test_ids_are_unique(self):
        notes = load_notes(str(FIXTURES))
        ids = [note.note_id for note in notes]
        assert len(ids) == len(set(ids))

    def test_body_is_loaded(self):
        notes = load_notes(str(FIXTURES))
        sourdough = next(n for n in notes if "sourdough" in n.path)
        assert "starter" in sourdough.body.lower()

    def test_missing_vault_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_notes(str(tmp_path / "does-not-exist"))

    def test_skips_secret_and_ignore(self, tmp_path: Path):
        (tmp_path / "keep.md").write_text("plain body")
        (tmp_path / "secret.md").write_text("this is #secret")
        (tmp_path / "ignore.md").write_text("this is #ignore")
        notes = load_notes(str(tmp_path))
        assert [n.path for n in notes] == ["keep.md"]

    def test_skips_frontmatter_ignore_tags(self, tmp_path: Path):
        (tmp_path / "keep.md").write_text("plain body")
        (tmp_path / "fm_secret.md").write_text(
            "---\ntags: [secret]\n---\nno body tag here\n"
        )
        (tmp_path / "fm_hash_ignore.md").write_text(
            "---\ntags: ['#ignore', other]\n---\nno body tag here\n"
        )
        notes = load_notes(str(tmp_path))
        assert [n.path for n in notes] == ["keep.md"]

    def test_skips_undecodable_file(self, tmp_path: Path):
        (tmp_path / "keep.md").write_text("plain body")
        (tmp_path / "broken.md").write_bytes(b"\xff\xfe not utf-8 \xff")
        notes = load_notes(str(tmp_path))
        assert [n.path for n in notes] == ["keep.md"]

    def test_skips_reserved_dirs(self, tmp_path: Path):
        (tmp_path / "keep.md").write_text("body")
        for reserved in (".trash", ".obsidian", "Templates"):
            d = tmp_path / reserved
            d.mkdir()
            (d / "x.md").write_text("skipme")
        notes = load_notes(str(tmp_path))
        assert [n.path for n in notes] == ["keep.md"]
