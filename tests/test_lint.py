"""Tests for vault_rag.compounding.lint."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from vault_rag.compounding.lint import extract_wikilinks, lint_vault

VALID_TS = "2025-01-01T00:00:00Z"


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def lint_dir(tmp_path: Path) -> Path:
    def fm(note_id, created=VALID_TS, updated=VALID_TS, extra=""):
        return f"---\nid: {note_id}\ncreated: {created}\nupdated: {updated}\n{extra}---\n"

    # A links to B, to a nonexistent note, plus a fenced + backtick link that must be ignored.
    write(tmp_path / "A.md", fm("01A000000000000000000000AA", updated="2025-06-01T00:00:00Z")
          + "Links to [[B]] and [[Nonexistent]].\n\n"
          + "```\n[[FencedLink]]\n```\n\n"
          + "Inline `[[BacktickLink]]` here.\n")
    write(tmp_path / "B.md", fm("01B000000000000000000000BB") + "Back to [[A]].\n")

    # No frontmatter -> missing id/created/updated. Links to A so it is not an orphan.
    write(tmp_path / "missing.md", "Just a body linking [[A]].\n")

    # Unparseable created + naive date.
    write(tmp_path / "badts.md",
          fm("01C000000000000000000000CC", created="yesterday", extra="date: 2024-05-01\n")
          + "See [[A]].\n")

    # Duplicate id shared by two notes; cross-linked so neither is an orphan.
    write(tmp_path / "dup1.md", fm("01D000000000000000000000DD") + "[[dup2]]\n")
    write(tmp_path / "dup2.md", fm("01D000000000000000000000DD") + "[[dup1]]\n")

    # No links in or out -> orphan.
    write(tmp_path / "orphan.md", fm("01E000000000000000000000EE") + "Nothing links here.\n")

    # Distilled note older than its source (A, updated 2025-06) -> stale.
    write(tmp_path / "distilled_stale.md",
          fm("01F000000000000000000000FF", updated="2024-01-01T00:00:00Z", extra="type: distilled\n")
          + "# Q\n\nanswer\n\n## Sources\n- [[A]]\n")

    # Distilled note newer than its source (B, updated 2025-01) -> not stale.
    write(tmp_path / "distilled_fresh.md",
          fm("01G000000000000000000000GG", updated="2025-12-01T00:00:00Z", extra="type: distilled\n")
          + "# Q\n\nanswer\n\n## Sources\n- [[B]]\n")

    return tmp_path


class TestExtractWikilinks:
    def test_alias_and_heading_links(self):
        links = extract_wikilinks("See [[Target|shown]] and [[Other#Section]].")
        targets = [t for t, _ in links]
        assert targets == ["Target", "Other"]

    def test_ignores_fenced_and_backtick(self):
        body = "Real [[Here]].\n```\n[[InFence]]\n```\n`[[InCode]]`\n"
        targets = [t for t, _ in extract_wikilinks(body)]
        assert targets == ["Here"]

    def test_line_numbers(self):
        body = "line1\n[[Two]]\n"
        assert extract_wikilinks(body) == [("Two", 2)]


class TestLint:
    def test_missing_frontmatter(self, lint_dir):
        report = lint_vault(str(lint_dir))
        paths = {f["path"] for f in report["findings"]["missing_frontmatter_fields"]}
        assert paths == {"missing.md"}
        entry = report["findings"]["missing_frontmatter_fields"][0]
        assert set(entry["missing"]) == {"id", "created", "updated"}

    def test_invalid_timestamps(self, lint_dir):
        report = lint_vault(str(lint_dir))
        entries = report["findings"]["invalid_timestamps"]
        by = {(e["path"], e["field"]): e["problem"] for e in entries}
        assert by[("badts.md", "created")] == "unparseable"
        assert by[("badts.md", "date")] == "naive"
        # No valid note is flagged.
        assert all(e["path"] == "badts.md" for e in entries)

    def test_duplicate_ids(self, lint_dir):
        report = lint_vault(str(lint_dir))
        dups = report["findings"]["duplicate_ids"]
        assert len(dups) == 1
        assert dups[0]["paths"] == ["dup1.md", "dup2.md"]

    def test_broken_wikilinks(self, lint_dir):
        report = lint_vault(str(lint_dir))
        broken = report["findings"]["broken_wikilinks"]
        targets = {b["target"] for b in broken}
        assert targets == {"Nonexistent"}

    def test_orphans(self, lint_dir):
        report = lint_vault(str(lint_dir))
        orphans = {o["path"] for o in report["findings"]["orphans"]}
        assert orphans == {"orphan.md"}

    def test_stale_distilled(self, lint_dir):
        report = lint_vault(str(lint_dir))
        stale = report["findings"]["stale_distilled"]
        flagged = {s["path"] for s in stale if "stale_sources" in s}
        assert flagged == {"distilled_stale.md"}

    def test_makes_no_writes(self, lint_dir):
        def checksum():
            h = hashlib.sha256()
            for p in sorted(lint_dir.rglob("*.md")):
                h.update(p.read_bytes())
            return h.hexdigest()

        before = checksum()
        lint_vault(str(lint_dir))
        assert checksum() == before

    def test_counts(self, lint_dir):
        report = lint_vault(str(lint_dir))
        # 9 markdown files, none ignored.
        assert report["notes_scanned"] == 9
        assert report["notes_ignored"] == 0
