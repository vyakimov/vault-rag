"""Tests for vault_spider.compounding.lint."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from vault_spider import cli
from vault_spider.compounding.backfill_core import ULID_RE
from vault_spider.compounding.lint import (
    extract_frontmatter_wikilinks,
    extract_wikilinks,
    lint_vault,
)
from vault_spider.corpus.frontmatter import coerce_datetime, split_frontmatter

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
    def test_duplicate_titles_case_insensitive(self, tmp_path):
        write(tmp_path / "first.md", "---\ntitle: Same\n---\nFirst.\n")
        write(tmp_path / "second.md", "---\ntitle: same\n---\nSecond.\n")

        report = lint_vault(str(tmp_path))

        assert report["findings"]["duplicate_titles"] == [
            {"title": "Same", "paths": ["first.md", "second.md"]}
        ]

    def test_unique_titles_have_no_finding(self, tmp_path):
        write(tmp_path / "first.md", "---\ntitle: First\n---\nBody.\n")
        write(tmp_path / "second.md", "---\ntitle: Second\n---\nBody.\n")
        assert lint_vault(str(tmp_path))["findings"]["duplicate_titles"] == []

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


class TestExtractFrontmatterWikilinks:
    def test_reads_links_from_values(self):
        block = 'parents: "[[Daily Notes]]"\ntags:\n  - x\n'
        assert extract_frontmatter_wikilinks(block) == [("Daily Notes", 2)]

    def test_no_links_is_empty(self):
        assert extract_frontmatter_wikilinks("tags:\n  - x\n") == []


class TestLinkResolution:
    def test_attachment_links_are_not_broken(self, tmp_path):
        """`[[diagram.png]]` names a real file — an attachment, not a missing note."""
        (tmp_path / "!attachments").mkdir()
        (tmp_path / "!attachments" / "diagram.png").write_bytes(b"\x89PNG")
        write(tmp_path / "A.md", "Embedded ![[diagram.png]] and [[missing.png]].\n")

        broken = lint_vault(str(tmp_path))["findings"]["broken_wikilinks"]

        assert {b["target"] for b in broken} == {"missing.png"}

    def test_hidden_dir_files_are_not_attachments(self, tmp_path):
        """`.git/config` is not linkable; `[[config]]` must stay a broken link."""
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("[core]\n")
        write(tmp_path / "A.md", "See [[config]].\n")

        broken = lint_vault(str(tmp_path))["findings"]["broken_wikilinks"]

        assert {b["target"] for b in broken} == {"config"}

    def test_links_to_excalidraw_drawings_resolve(self, tmp_path):
        """Drawings are skipped as notes, but they are real files — links to them still resolve."""
        (tmp_path / "Excalidraw").mkdir()
        (tmp_path / "Excalidraw" / "map0.excalidraw.md").write_text(
            "---\nexcalidraw-plugin: parsed\n---\ncompressed\n"
        )
        write(tmp_path / "A.md", "See [[map0.excalidraw]] and [[map0.excalidraw.md]].\n")

        assert lint_vault(str(tmp_path))["findings"]["broken_wikilinks"] == []

    def test_alias_links_resolve(self, tmp_path):
        write(tmp_path / "Obsidian MOC.md", "---\naliases:\n  - Obsidian\n---\nHub.\n")
        write(tmp_path / "A.md", "See [[Obsidian]].\n")

        report = lint_vault(str(tmp_path))

        assert report["findings"]["broken_wikilinks"] == []
        # The alias link is a real edge: neither note is an orphan.
        assert report["findings"]["orphans"] == []

    def test_multiword_alias_is_one_alias(self, tmp_path):
        write(tmp_path / "PGx.md", "---\naliases:\n  - Pharmacogenetics Hub\n---\nHub.\n")
        write(tmp_path / "A.md", "See [[Pharmacogenetics Hub]].\n")

        assert lint_vault(str(tmp_path))["findings"]["broken_wikilinks"] == []

    def test_frontmatter_link_counts_as_an_edge(self, tmp_path):
        """A daily note whose only link is `parents:` is a child, not an orphan."""
        write(tmp_path / "Daily Notes.md", "The hub.\n")
        write(tmp_path / "2026-07-10.md", '---\nparents: "[[Daily Notes]]"\n---\n\n# 2026-07-10\n')

        report = lint_vault(str(tmp_path))

        assert report["findings"]["orphans"] == []
        assert report["findings"]["broken_wikilinks"] == []

    def test_broken_frontmatter_link_is_reported(self, tmp_path):
        write(tmp_path / "note.md", '---\nparents: "[[Nowhere]]"\n---\nBody.\n')

        broken = lint_vault(str(tmp_path))["findings"]["broken_wikilinks"]

        assert len(broken) == 1
        assert broken[0]["target"] == "Nowhere"
        assert broken[0]["location"] == "frontmatter"

    def test_body_links_are_located(self, tmp_path):
        write(tmp_path / "note.md", "Link to [[Nowhere]].\n")
        broken = lint_vault(str(tmp_path))["findings"]["broken_wikilinks"]
        assert broken[0]["location"] == "body"


class TestNewChecks:
    def test_dangling_targets_rank_by_link_count(self, tmp_path):
        write(tmp_path / "a.md", "[[Wanted]] and [[Other]].\n")
        write(tmp_path / "b.md", "[[Wanted]] again.\n")

        dangling = lint_vault(str(tmp_path))["findings"]["dangling_targets"]

        assert dangling[0] == {
            "target": "Wanted",
            "count": 2,
            "linked_from": ["a.md", "b.md"],
        }
        assert dangling[1]["target"] == "Other"

    def test_empty_notes_rank_by_inbound_links(self, tmp_path):
        write(tmp_path / "stub.md", "---\nid: x\n---\n")           # empty, 2 inbound
        write(tmp_path / "lonely.md", "---\nid: y\n---\n\n")       # empty, 0 inbound
        write(tmp_path / "a.md", "A real note that happens to link to [[stub]].\n")
        write(tmp_path / "b.md", "Another real note linking to [[stub]] as well.\n")

        empty = lint_vault(str(tmp_path))["findings"]["empty_notes"]

        # The most-linked stub sorts first: it is the most valuable one to write.
        assert [e["path"] for e in empty] == ["stub.md", "lonely.md"]
        assert empty[0] == {"path": "stub.md", "chars": 0, "inbound": 2}

    def test_note_with_content_is_not_empty(self, tmp_path):
        write(tmp_path / "real.md", "This body is comfortably longer than the stub cutoff.\n")
        assert lint_vault(str(tmp_path))["findings"]["empty_notes"] == []

    def test_conflict_copies(self, tmp_path):
        write(tmp_path / "Note.md", "Shared body text here.\nPlus one extra line.\n")
        write(tmp_path / "Note 1.md", "Shared body text here.\n")
        write(tmp_path / "Standalone 2.md", "No base note exists for this one.\n")

        copies = lint_vault(str(tmp_path))["findings"]["conflict_copies"]

        assert len(copies) == 1
        assert copies[0]["path"] == "Note 1.md"
        assert copies[0]["base_path"] == "Note.md"
        assert 0.0 < copies[0]["similarity"] <= 1.0

    def test_excalidraw_files_are_not_linted(self, tmp_path):
        write(tmp_path / "Drawing.excalidraw.md", "---\nid: z\n---\ncompressed-json-blob\n")
        report = lint_vault(str(tmp_path))
        assert report["notes_scanned"] == 0
        assert report["notes_ignored"] == 1


def test_cli_fix_timestamps_normalizes_naive_values(capsys, tmp_path):
    note = tmp_path / "daily.md"
    body = "# 2026-07-10\n\nLogbook.\n"
    write(note, f'---\nid: 01H\nparents: "[[x]]"\ndate: 2026-07-10T10:01\n---\n{body}')

    code = cli.main(["lint", "--root", str(tmp_path), "--fix-timestamps"])
    envelope = json.loads(capsys.readouterr().out)
    frontmatter, _ = split_frontmatter(note.read_text(encoding="utf-8"))

    assert code == 0
    assert envelope["result"]["summary"]["invalid_timestamps"] == 0
    assert envelope["result"]["fixed"][0]["path"] == "daily.md"
    resolved = coerce_datetime(frontmatter["date"])
    assert resolved.tzinfo is not None
    # Local wall-clock time is preserved; only the offset is attached.
    assert (resolved.hour, resolved.minute) == (10, 1)
    # Untouched keys and the body survive byte-for-byte.
    assert frontmatter["parents"] == "[[x]]"
    assert note.read_text(encoding="utf-8").endswith(body)


def test_cli_fix_timestamps_skips_unparseable(capsys, tmp_path):
    note = tmp_path / "bad.md"
    original = "---\nid: 01H\ncreated: yesterday\n---\nBody.\n"
    write(note, original)

    code = cli.main(["lint", "--root", str(tmp_path), "--fix-timestamps"])
    envelope = json.loads(capsys.readouterr().out)

    assert code == 0
    assert envelope["result"]["fixed"] == []
    assert envelope["result"]["fix_skipped"] == [
        {"path": "bad.md", "field": "created", "reason": "unparseable"}
    ]
    assert note.read_text(encoding="utf-8") == original


def test_cli_fix_missing_contract_fields(capsys, tmp_path):
    note = tmp_path / "missing.md"
    body = "Body stays byte-identical.\nSecond line.\n"
    write(note, body)

    code = cli.main(["lint", "--root", str(tmp_path), "--fix"])
    envelope = json.loads(capsys.readouterr().out)
    updated_raw = note.read_text(encoding="utf-8")
    frontmatter, _ = split_frontmatter(updated_raw)

    assert code == 0
    assert envelope["result"]["summary"]["missing_frontmatter_fields"] == 0
    assert envelope["result"]["fixed"] == [
        {"path": "missing.md", "fields": ["id", "created", "updated"]}
    ]
    assert ULID_RE.match(str(frontmatter["id"]))
    assert coerce_datetime(frontmatter["created"]).tzinfo is not None
    assert coerce_datetime(frontmatter["updated"]).tzinfo is not None
    assert updated_raw.endswith(body)


def test_cli_fix_skips_unparseable_frontmatter(capsys, tmp_path):
    note = tmp_path / "bad.md"
    original = "---\ntitle: [broken\n---\nBody.\n"
    write(note, original)

    code = cli.main(["lint", "--root", str(tmp_path), "--fix"])
    envelope = json.loads(capsys.readouterr().out)

    assert code == 0
    assert envelope["result"]["fix_skipped"] == [
        {"path": "bad.md", "reason": "frontmatter present but failed to parse"}
    ]
    assert note.read_text(encoding="utf-8") == original


def test_cli_lint_text_format_is_readable(capsys, tmp_path):
    """`--format text` must render findings as lines, never as raw Python dicts."""
    write(tmp_path / "a.md", "---\nid: x\n---\nLinks to [[Nowhere]] twice: [[Nowhere]].\n")
    write(tmp_path / "stub.md", "---\nid: y\n---\n")

    code = cli.main(["lint", "--root", str(tmp_path), "--format", "text"])
    out = capsys.readouterr().out

    assert code == 0
    assert "2x  [[Nowhere]]" in out          # aggregated, not one line per occurrence
    assert "empty notes" in out
    assert "{'path'" not in out              # no dict repr leaked into the report


def test_cli_lint_text_reports_a_clean_vault(capsys, tmp_path):
    fm = "---\nid: {}\ncreated: 2025-01-01T00:00:00Z\nupdated: 2025-01-01T00:00:00Z\n---\n"
    write(tmp_path / "a.md", fm.format("x") + "A real note with enough body to not be a stub. See [[b]].\n")
    write(tmp_path / "b.md", fm.format("y") + "Another real note with a decent body. See [[a]].\n")

    cli.main(["lint", "--root", str(tmp_path), "--format", "text"])

    assert "No findings. The vault is clean." in capsys.readouterr().out
