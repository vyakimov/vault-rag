"""Tests for vault_spider.corpus.chunker.split_sections."""

from __future__ import annotations

from vault_spider.corpus.chunker import section_text, split_sections
from vault_spider.corpus.loader import Note


def make_note(body: str, note_id: str = "n") -> Note:
    return Note(
        note_id=note_id,
        path="p.md",
        title="T",
        tags=[],
        created=None,
        updated=None,
        date="2024-01-01T00:00:00+00:00",
        note_type="",
        body=body,
        raw_text=body,
        content_hash="h",
    )


def _covered_lines(sections):
    covered = set()
    for section in sections:
        covered |= set(range(section.line_start, section.line_end + 1))
    return covered


class TestHeadingSplit:
    def test_levels_one_to_three_split_h4_stays_inside(self):
        body = (
            "Preamble line.\n\n"
            "# H1\n"
            "content1\n\n"
            "## H2\n"
            "content2\n\n"
            "#### H4 inside\n"
            "h4 body\n\n"
            "### H3\n"
            "content3\n"
        )
        sections = split_sections(make_note(body))
        assert [s.heading for s in sections] == ["", "H1", "H2", "H3"]
        assert [s.level for s in sections] == [0, 1, 2, 3]
        h2 = sections[2]
        assert "#### H4 inside" in h2.text
        assert "h4 body" in h2.text

    def test_preamble_only_when_non_blank(self):
        body = "\n# H1\nbody\n"
        sections = split_sections(make_note(body))
        assert [s.heading for s in sections] == ["H1"]

    def test_chunk_ids_are_sequential(self):
        body = "# A\nx\n## B\ny\n### C\nz\n"
        sections = split_sections(make_note(body, note_id="abc"))
        assert [s.chunk_id for s in sections] == ["abc::s000", "abc::s001", "abc::s002"]


class TestFencedCode:
    def test_hash_inside_fence_is_not_a_heading(self):
        body = (
            "# Real heading\n"
            "text\n\n"
            "```python\n"
            "# not a heading\n"
            "x = 1\n"
            "```\n"
            "after\n"
        )
        sections = split_sections(make_note(body))
        assert len(sections) == 1
        assert sections[0].heading == "Real heading"
        assert "# not a heading" in sections[0].text


class TestNoHeadings:
    def test_single_section(self):
        body = "line one\nline two\nline three"
        sections = split_sections(make_note(body))
        assert len(sections) == 1
        assert sections[0].heading == ""
        assert sections[0].level == 0
        assert sections[0].line_start == 1
        assert sections[0].line_end == 3


class TestOversizeSplit:
    def test_splits_with_overlap_and_correct_ranges(self):
        body = "\n".join("aaaa" for _ in range(10))
        note = make_note(body)
        sections = split_sections(note, max_chars=10, overlap_chars=5)
        assert len(sections) > 1

        body_lines = body.split("\n")
        # Each window respects max_chars (these lines are short) and text matches ranges.
        for section in sections:
            expected = "\n".join(body_lines[section.line_start - 1 : section.line_end])
            assert section.text == expected
            assert len(section.text) <= 10

        # Consecutive windows overlap.
        assert any(
            sections[i + 1].line_start <= sections[i].line_end
            for i in range(len(sections) - 1)
        )

    def test_every_body_line_covered(self):
        body = "\n".join(f"line-{i}" for i in range(1, 13))
        sections = split_sections(make_note(body), max_chars=20, overlap_chars=8)
        assert _covered_lines(sections) == set(range(1, 13))


class TestLineCoverageProperty:
    def test_all_lines_in_a_section_with_headings(self):
        body = (
            "intro\n"
            "# One\n"
            "a\nb\nc\n"
            "## Two\n"
            "d\ne\n"
        )
        sections = split_sections(make_note(body))
        n = len(body.split("\n"))
        assert _covered_lines(sections) == set(range(1, n + 1))


class TestSectionText:
    def test_composition(self):
        sections = split_sections(make_note("# H\nbody"))
        text = section_text(make_note("# H\nbody"), sections[0])
        assert text.startswith("# T")
        assert "Section: H" in text
