"""Tests for the vault-rag CLI, driven in-process with a fake provider."""

from __future__ import annotations

import json

from vault_rag import cli


def run(capsys, argv):
    code = cli.main(argv)
    out = capsys.readouterr().out.strip()
    return code, json.loads(out)


class TestSchema:
    def test_schema_is_stable(self, capsys):
        code, envelope = run(capsys, ["schema"])
        assert code == 0
        assert envelope["ok"] is True
        assert envelope["action"] == "schema"
        assert envelope["result"]["version"] == 1
        assert "retrieval_output" in envelope["result"]["contracts"]
        assert "synthesis_output" in envelope["result"]["contracts"]


class TestEnvelopeShape:
    def test_success_envelope_keys(self, capsys, tmp_path, tiny_vault, fake_provider, monkeypatch):
        monkeypatch.setattr(cli, "get_provider", lambda: fake_provider)
        chroma = str(tmp_path / "chroma")
        code, envelope = run(
            capsys, ["--chroma-path", chroma, "sync", "--root", str(tiny_vault)]
        )
        assert code == 0
        assert set(["ok", "action", "result", "meta"]).issubset(envelope.keys())
        assert envelope["ok"] is True
        assert envelope["action"] == "sync"
        assert envelope["result"]["added_notes"] == 5

    def test_chroma_path_after_subcommand(
        self, capsys, tmp_path, tiny_vault, fake_provider, monkeypatch
    ):
        monkeypatch.setattr(cli, "get_provider", lambda: fake_provider)
        chroma = tmp_path / "after"

        code, envelope = run(
            capsys,
            ["sync", "--chroma-path", str(chroma), "--root", str(tiny_vault)],
        )

        assert code == 0
        assert envelope["ok"] is True
        assert chroma.exists()

    def test_chroma_path_default(
        self, capsys, tmp_path, tiny_vault, fake_provider, monkeypatch
    ):
        monkeypatch.setattr(cli, "get_provider", lambda: fake_provider)
        monkeypatch.chdir(tmp_path)

        code, envelope = run(capsys, ["sync", "--root", str(tiny_vault)])

        assert code == 0
        assert envelope["ok"] is True
        assert (tmp_path / "chroma_db").exists()


class TestSyncAndRetrieve:
    def test_retrieve_returns_candidates(self, capsys, tmp_path, tiny_vault, fake_provider, monkeypatch):
        monkeypatch.setattr(cli, "get_provider", lambda: fake_provider)
        chroma = str(tmp_path / "chroma")
        run(capsys, ["--chroma-path", chroma, "sync", "--root", str(tiny_vault)])

        code, envelope = run(
            capsys,
            ["--chroma-path", chroma, "retrieve", "--query", "zqxq", "--granularity", "section"],
        )
        assert code == 0
        assert envelope["ok"] is True
        assert envelope["action"] == "retrieve"
        assert envelope["result"]["candidates"]
        assert "timing_ms" in envelope["meta"]


class TestEmptyIndexError:
    def test_retrieve_on_empty_index(self, capsys, tmp_path, fake_provider, monkeypatch):
        monkeypatch.setattr(cli, "get_provider", lambda: fake_provider)
        chroma = str(tmp_path / "empty-chroma")
        code, envelope = run(
            capsys, ["--chroma-path", chroma, "retrieve", "--query", "anything"]
        )
        assert code == 1
        assert envelope["ok"] is False
        assert envelope["error"]["type"] == "index_empty"


class TestStats:
    def test_missing_index_is_empty(self, capsys, tmp_path):
        code, envelope = run(
            capsys, ["stats", "--chroma-path", str(tmp_path / "missing")]
        )

        assert code == 1
        assert envelope["error"]["type"] == "index_empty"

    def test_reports_index_without_provider_key(
        self, capsys, tmp_path, tiny_vault, fake_provider, monkeypatch
    ):
        chroma = str(tmp_path / "chroma")
        monkeypatch.setattr(cli, "get_provider", lambda: fake_provider)
        run(capsys, ["sync", "--chroma-path", chroma, "--root", str(tiny_vault)])
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        code, envelope = run(capsys, ["stats", "--chroma-path", chroma])

        assert code == 0
        assert envelope["result"]["total_documents"] == 5
        assert envelope["result"]["section_entries"] >= 5
        assert envelope["result"]["embedding_model"] == "fake-embed"


class TestInvalidArguments:
    def test_sync_missing_root(self, capsys, tmp_path, fake_provider, monkeypatch):
        monkeypatch.setattr(cli, "get_provider", lambda: fake_provider)
        code, envelope = run(
            capsys,
            ["--chroma-path", str(tmp_path / "c"), "sync", "--root", str(tmp_path / "nope")],
        )
        assert code == 1
        assert envelope["ok"] is False
        assert envelope["error"]["type"] == "invalid_arguments"

    def test_sync_reset_and_dry_run_are_incompatible(
        self, capsys, tiny_vault, monkeypatch
    ):
        monkeypatch.setattr(
            cli, "get_provider", lambda: (_ for _ in ()).throw(AssertionError())
        )

        code, envelope = run(
            capsys, ["sync", "--root", str(tiny_vault), "--reset", "--dry-run"]
        )

        assert code == 1
        assert envelope["error"]["type"] == "invalid_arguments"


class TestSynthesizeFromRetrievalFile:
    def test_reads_prior_retrieval(self, capsys, tmp_path, fake_provider, monkeypatch):
        monkeypatch.setattr(cli, "get_provider", lambda: fake_provider)
        contract = {
            "query": "q",
            "mode": "fast",
            "granularity": "section",
            "candidates": [
                {
                    "note_id": "n1", "path": "a.md", "title": "A", "type": "",
                    "heading": "H", "chunk_id": "n1::s000", "line_start": 1, "line_end": 2,
                    "excerpt": "excerpt", "why": "combined keyword+semantic signal",
                    "scores": {"bm25": 1.0, "semantic": 1.0, "fused": 0.5, "reranker": None, "final": 0.5},
                }
            ],
        }
        retrieval_file = tmp_path / "r.json"
        retrieval_file.write_text(json.dumps(contract), encoding="utf-8")

        code, envelope = run(
            capsys, ["synthesize", "--query", "q", "--retrieval", str(retrieval_file)]
        )
        assert code == 0
        assert envelope["ok"] is True
        assert envelope["action"] == "synthesize"
        assert "retrieval" in envelope["result"]
        assert envelope["result"]["retrieval"]["candidates"]

    def test_replay_without_any_query_is_rejected(self, capsys, tmp_path, fake_provider, monkeypatch):
        monkeypatch.setattr(cli, "get_provider", lambda: fake_provider)
        retrieval_file = tmp_path / "r.json"
        retrieval_file.write_text(json.dumps({"candidates": []}), encoding="utf-8")

        code, envelope = run(capsys, ["synthesize", "--retrieval", str(retrieval_file)])
        assert code == 1
        assert envelope["ok"] is False
        assert envelope["error"]["type"] == "invalid_arguments"


class TestMissingApiKey:
    def test_missing_key_maps_to_provider_error(self, capsys, tmp_path, tiny_vault, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        code, envelope = run(
            capsys,
            ["--chroma-path", str(tmp_path / "c"), "sync", "--root", str(tiny_vault)],
        )
        assert code == 1
        assert envelope["ok"] is False
        assert envelope["error"]["type"] == "provider_error"
        assert "OPENROUTER_API_KEY" in envelope["error"]["message"]
