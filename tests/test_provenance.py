"""Tests for the provenance field: loading, filtering, synthesis framing,
distill stamping, lint checks, and the backfill tool."""

from __future__ import annotations

import json
from pathlib import Path

from tools.backfill_provenance import derive_provenance, stamp
from vault_spider import cli
from vault_spider.compounding.distill import render_distilled_note
from vault_spider.compounding.lint import lint_vault
from vault_spider.corpus.loader import load_notes
from vault_spider.synthesis.answer import build_context


def write_note(root: Path, rel: str, frontmatter: str, body: str = "Body text.\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


class TestLoader:
    def test_provenance_read_and_normalized(self, tmp_path):
        write_note(tmp_path, "a.md", "id: A\nprovenance: Reference\n")
        write_note(tmp_path, "b.md", "id: B\n")
        notes = {n.path: n for n in load_notes(str(tmp_path))}
        assert notes["a.md"].provenance == "reference"
        assert notes["b.md"].provenance == ""


class TestSynthesisFraming:
    def candidate(self, **overrides):
        base = {
            "title": "T", "path": "a.md", "excerpt": "text",
            "scores": {"final": 1.0}, "type": "", "provenance": "",
        }
        base.update(overrides)
        return base

    def context_for(self, candidate):
        context, _ = build_context({"candidates": [candidate]})
        return context

    def test_distilled_provenance_flagged(self):
        assert "provenance=distilled" in self.context_for(
            self.candidate(provenance="distilled")
        )

    def test_reference_and_llm_flagged(self):
        assert "provenance=reference" in self.context_for(
            self.candidate(provenance="reference")
        )
        assert "provenance=llm" in self.context_for(self.candidate(provenance="llm"))

    def test_type_distilled_fallback_for_unstamped_notes(self):
        assert "provenance=distilled" in self.context_for(
            self.candidate(type="distilled")
        )

    def test_human_and_unstamped_carry_no_attribute(self):
        assert "provenance=" not in self.context_for(self.candidate())
        assert "provenance=" not in self.context_for(
            self.candidate(provenance="human")
        )


class TestDistillStamp:
    def test_rendered_note_carries_provenance(self):
        text = render_distilled_note(
            {"question": "Q?", "answer": "A.", "citations": []}
        )
        assert "provenance: distilled" in text
        assert "type: distilled" in text


class TestLintChecks:
    def test_imported_missing_source_flagged(self, tmp_path):
        write_note(tmp_path, "clip.md", "id: A\nprovenance: reference\n")
        write_note(
            tmp_path, "chat.md",
            "id: B\nprovenance: llm\nsource_url: https://chat.example/1\n",
        )
        write_note(tmp_path, "mine.md", "id: C\nprovenance: human\n")
        report = lint_vault(str(tmp_path))
        flagged = [f["path"] for f in report["findings"]["imported_missing_source"]]
        assert flagged == ["clip.md"]

    def test_stale_and_orphan_checks_honor_provenance(self, tmp_path):
        # provenance: distilled without type: distilled is still exempt from
        # orphans and in scope for stale checking.
        write_note(
            tmp_path, "d.md",
            "id: D\nprovenance: distilled\n",
            "# Q\n\nAnswer.\n\n## Sources\n- [[Missing Note]]\n",
        )
        report = lint_vault(str(tmp_path))
        assert report["summary"]["orphans"] == 0
        assert report["summary"]["stale_distilled"] == 1


class TestRetrievalFilter:
    def test_provenance_filter(self, capsys, tmp_path, fake_provider, monkeypatch):
        monkeypatch.setattr(cli, "get_provider", lambda: fake_provider)
        vault = tmp_path / "vault"
        write_note(vault, "mine.md", "id: A\nprovenance: human\n", "zqxq human note\n")
        write_note(vault, "clip.md", "id: B\nprovenance: reference\n", "zqxq clipped note\n")
        chroma = str(tmp_path / "chroma")

        def run(argv):
            code = cli.main(argv)
            out = capsys.readouterr().out.strip()
            return code, json.loads(out)

        run(["--chroma-path", chroma, "sync", "--root", str(vault)])
        code, envelope = run(
            ["--chroma-path", chroma, "retrieve", "--query", "zqxq",
             "--provenance", "reference"]
        )
        assert code == 0
        paths = {c["path"] for c in envelope["result"]["candidates"]}
        assert paths == {"clip.md"}
        assert envelope["result"]["candidates"][0]["provenance"] == "reference"

    def test_invalid_provenance_rejected(self, capsys):
        code = cli.main(["retrieve", "--query", "x", "--provenance", "alien"])
        envelope = json.loads(capsys.readouterr().out.strip())
        assert code == 1
        assert envelope["error"]["type"] == "invalid_arguments"


class TestBackfillTool:
    def test_heuristics(self):
        assert derive_provenance({"type": "distilled"}) == "distilled"
        assert derive_provenance({"source_type": "llm"}) == "llm"
        assert derive_provenance({"source_type": "web"}) == "reference"
        assert derive_provenance({"source_url": "https://x.example/p"}) == "reference"
        assert derive_provenance({"type": "recipe"}) == "human"

    def test_stamp_preserves_body_and_frontmatter(self):
        raw = "---\nid: A\ntitle: T\n---\n# Body\n\ntext\n"
        stamped = stamp(raw, "human")
        assert stamped.endswith("# Body\n\ntext\n")
        assert "provenance: human\n---\n" in stamped

    def test_dry_run_and_apply(self, tmp_path, monkeypatch, capsys):
        import tools.backfill_provenance as tool

        write_note(tmp_path, "a.md", "id: A\ntype: distilled\n")
        write_note(tmp_path, "b.md", "id: B\nprovenance: human\n")
        (tmp_path / "bare.md").write_text("no frontmatter\n", encoding="utf-8")

        monkeypatch.setattr(
            "sys.argv", ["backfill_provenance", "--root", str(tmp_path)]
        )
        assert tool.main() == 0
        assert "provenance" not in (tmp_path / "a.md").read_text()

        monkeypatch.setattr(
            "sys.argv", ["backfill_provenance", "--root", str(tmp_path), "--apply"]
        )
        assert tool.main() == 0
        assert "provenance: distilled" in (tmp_path / "a.md").read_text()
        # already-set and frontmatter-less notes untouched
        assert (tmp_path / "b.md").read_text().count("provenance") == 1
        assert "provenance" not in (tmp_path / "bare.md").read_text()
