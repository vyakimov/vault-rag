import os
from datetime import datetime, timezone

from vault_spider import cli
from vault_spider.index.store import IndexStore
from vault_spider.retrieval.searcher import Searcher


def build_searcher(tmp_path, tiny_vault, fake_provider):
    store = IndexStore(str(tmp_path / "chroma"), provider=fake_provider)
    store.sync(str(tiny_vault))
    return store, Searcher(store, provider=fake_provider)


def paths(result):
    return {row["metadata"]["path"] for row in result.rows}


def test_type_filter(tmp_path, tiny_vault, fake_provider):
    _, searcher = build_searcher(tmp_path, tiny_vault, fake_provider)
    result = searcher.hybrid_search("alpha", note_type="recipe")
    assert paths(result) == {"note_a.md"}


def test_tag_filter(tmp_path, tiny_vault, fake_provider):
    _, searcher = build_searcher(tmp_path, tiny_vault, fake_provider)
    result = searcher.hybrid_search("gamma", tags=["gamma"])
    assert paths(result) == {"note_updated.md"}


def test_folder_filter(tmp_path, tiny_vault, fake_provider):
    subfolder = tiny_vault / "Projects" / "Sub"
    subfolder.mkdir(parents=True)
    (subfolder / "project.md").write_text("Project-specific content.\n", encoding="utf-8")
    _, searcher = build_searcher(tmp_path, tiny_vault, fake_provider)
    result = searcher.hybrid_search("project", folder="Projects")
    assert paths(result) == {"Projects/Sub/project.md"}


def test_date_filters_exclude_missing_dates(tmp_path, tiny_vault, fake_provider):
    old_timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    for name in ("note_plain.md", "note_code.md"):
        os.utime(tiny_vault / name, (old_timestamp, old_timestamp))
    _, searcher = build_searcher(tmp_path, tiny_vault, fake_provider)
    recent = searcher.hybrid_search("content", since="2025-01-01")
    older = searcher.hybrid_search("content", until="2024-12-31")
    assert paths(recent) == {"note_updated.md"}
    assert "note_updated.md" not in paths(older)
    assert paths(older) == {
        "note_a.md",
        "note_big.md",
        "note_code.md",
        "note_plain.md",
    }


def test_impossible_filter_raises(tmp_path, tiny_vault, fake_provider):
    _, searcher = build_searcher(tmp_path, tiny_vault, fake_provider)
    try:
        searcher.hybrid_search("anything", tags=["nope"])
    except ValueError as exc:
        assert str(exc) == "No documents match the required filters."
    else:
        raise AssertionError("expected ValueError")


def test_cli_tag_passthrough(capsys, tmp_path, tiny_vault, fake_provider, monkeypatch):
    monkeypatch.setattr(cli, "get_provider", lambda: fake_provider)
    chroma = str(tmp_path / "chroma")
    cli.main(["sync", "--chroma-path", chroma, "--root", str(tiny_vault)])
    capsys.readouterr()

    code = cli.main(
        ["retrieve", "--chroma-path", chroma, "--query", "gamma", "--tag", "gamma"]
    )
    envelope = __import__("json").loads(capsys.readouterr().out)

    assert code == 0
    assert {candidate["path"] for candidate in envelope["result"]["candidates"]} == {
        "note_updated.md"
    }
    assert envelope["meta"]["tunables"]["filters"]["tags"] == ["gamma"]


def test_cli_invalid_date_is_invalid_arguments(capsys):
    code = cli.main(["retrieve", "--query", "q", "--since", "not-a-date"])
    envelope = __import__("json").loads(capsys.readouterr().out)
    assert code == 1
    assert envelope["error"]["type"] == "invalid_arguments"
