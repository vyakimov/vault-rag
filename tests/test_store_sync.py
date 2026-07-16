"""Tests for vault_spider.index.store.IndexStore.sync against a real Chroma db."""

from __future__ import annotations

from pathlib import Path

import pytest

from vault_spider.index.store import IndexStore
from vault_spider.llm.openrouter import OpenRouterError


def build_store(chroma_dir: Path, provider) -> IndexStore:
    return IndexStore(
        chroma_db_path=str(chroma_dir),
        collection_name="vault_notes",
        provider=provider,
    )


def test_initial_sync_adds_doc_and_section_entries(tmp_path, tiny_vault, fake_provider):
    store = build_store(tmp_path / "chroma", fake_provider)
    result = store.sync(str(tiny_vault))

    assert result["added_notes"] == 5  # note_secret is skipped
    assert result["updated_notes"] == 0
    assert result["deleted_notes"] == 0

    # One document entry per note, several section entries.
    assert len(store.metadatas["document"]) == 5
    assert len(store.metadatas["section"]) >= 5
    assert result["total_entries"] == store.collection.count()
    assert result["total_entries"] == (
        len(store.metadatas["document"]) + len(store.metadatas["section"])
    )


def test_secret_note_absent(tmp_path, tiny_vault, fake_provider):
    store = build_store(tmp_path / "chroma", fake_provider)
    store.sync(str(tiny_vault))
    titles = {m.get("title") for m in store.metadatas["document"]}
    assert "Secret note" not in titles


def test_editing_reembeds_only_that_note(tmp_path, tiny_vault, fake_provider):
    store = build_store(tmp_path / "chroma", fake_provider)
    store.sync(str(tiny_vault))
    fake_provider.embed_calls.clear()

    (tiny_vault / "note_plain.md").write_text(
        "Completely different plain content now.", encoding="utf-8"
    )
    result = store.sync(str(tiny_vault))

    assert result["updated_notes"] == 1
    assert result["unchanged"] == 4
    assert result["added_notes"] == 0
    # A single embed call containing only the edited note's entries (1 doc + 1 section).
    assert len(fake_provider.embed_calls) == 1
    assert len(fake_provider.embed_calls[0]) == 2


def test_deleting_a_file_removes_its_entries(tmp_path, tiny_vault, fake_provider):
    store = build_store(tmp_path / "chroma", fake_provider)
    store.sync(str(tiny_vault))

    (tiny_vault / "note_big.md").unlink()
    result = store.sync(str(tiny_vault))

    assert result["deleted_notes"] == 1
    titles = {m.get("title") for m in store.metadatas["document"]}
    assert "Big note" not in titles


def test_second_sync_is_noop(tmp_path, tiny_vault, fake_provider):
    store = build_store(tmp_path / "chroma", fake_provider)
    store.sync(str(tiny_vault))
    fake_provider.embed_calls.clear()

    result = store.sync(str(tiny_vault))
    assert result["unchanged"] == 5
    assert result["added_notes"] == 0
    assert result["updated_notes"] == 0
    assert result["deleted_notes"] == 0
    # No entries added -> no embedding work.
    assert fake_provider.embed_calls == []


def test_rehydrate_on_new_store_instance(tmp_path, tiny_vault, fake_provider):
    build_store(tmp_path / "chroma", fake_provider).sync(str(tiny_vault))

    fresh = build_store(tmp_path / "chroma", fake_provider)
    assert len(fresh.metadatas["document"]) == 5
    assert fresh.bm25["document"] is None
    assert fresh.bm25["section"] is None

    fresh.granularity_data("document")
    assert fresh.bm25["document"] is not None
    assert fresh.bm25["section"] is None

    fresh.granularity_data("section")
    assert fresh.bm25["document"] is not None
    assert fresh.bm25["section"] is not None


def test_moving_a_note_updates_path_metadata(tmp_path, tiny_vault, fake_provider):
    # A frontmatter id keeps note_id stable across the move; only the path changes.
    (tiny_vault / "note_moved.md").write_text(
        "---\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntitle: Movable\n---\nSame body.\n",
        encoding="utf-8",
    )
    store = build_store(tmp_path / "chroma", fake_provider)
    store.sync(str(tiny_vault))
    fake_provider.embed_calls.clear()

    archive = tiny_vault / "Archive"
    archive.mkdir()
    (tiny_vault / "note_moved.md").rename(archive / "note_moved.md")
    result = store.sync(str(tiny_vault))

    assert result["updated_notes"] == 1
    assert result["deleted_notes"] == 0
    assert result["added_notes"] == 0
    paths = {m.get("path") for m in store.metadatas["document"]}
    assert "Archive/note_moved.md" in paths
    assert "note_moved.md" not in paths
    assert len(fake_provider.embed_calls) == 1
    assert len(fake_provider.embed_calls[0]) == 1


def test_section_embeddings_are_reused_for_small_edits(
    tmp_path, tiny_vault, fake_provider
):
    store = build_store(tmp_path / "chroma", fake_provider)
    store.sync(str(tiny_vault))
    initial_count = store.collection.count()
    fake_provider.embed_calls.clear()

    note = tiny_vault / "note_a.md"
    note.write_text(
        note.read_text(encoding="utf-8") + "Changed detail marker.\n",
        encoding="utf-8",
    )
    result = store.sync(str(tiny_vault))

    assert result["updated_notes"] == 1
    assert len(fake_provider.embed_calls) == 1
    embedded = fake_provider.embed_calls[0]
    assert len(embedded) == 2
    assert all("Changed detail marker." in text for text in embedded)
    assert not any("# Overview\nAlpha overview paragraph." == text for text in embedded)
    assert store.collection.count() == initial_count
    assert store.granularity_data("section")[3] is not None


def test_duplicate_frontmatter_ids_skip_later_note(tmp_path, fake_provider):
    vault = tmp_path / "dupvault"
    vault.mkdir()
    (vault / "a.md").write_text(
        "---\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntitle: First\n---\nBody A.\n",
        encoding="utf-8",
    )
    (vault / "b.md").write_text(
        "---\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntitle: Second\n---\nBody B.\n",
        encoding="utf-8",
    )

    store = build_store(tmp_path / "chroma", fake_provider)
    result = store.sync(str(vault))

    assert result["added_notes"] == 1
    assert len(result["warnings"]) == 1
    assert "duplicate note id" in result["warnings"][0]
    assert "b.md" in result["warnings"][0]
    titles = {m.get("title") for m in store.metadatas["document"]}
    assert titles == {"First"}


def test_dry_run_predicts_sync_without_mutation(
    tmp_path, tiny_vault, fake_provider
):
    store = build_store(tmp_path / "chroma", fake_provider)
    store.sync(str(tiny_vault))
    initial_count = store.collection.count()
    fake_provider.embed_calls.clear()

    (tiny_vault / "note_plain.md").write_text("Changed beta body.\n", encoding="utf-8")
    (tiny_vault / "note_code.md").unlink()
    (tiny_vault / "note_new.md").write_text("A newly added note.\n", encoding="utf-8")

    dry = store.sync(str(tiny_vault), dry_run=True)

    assert dry["would_add"] == ["note_new.md"]
    assert dry["would_update"] == ["note_plain.md"]
    assert dry["would_delete"] == ["note_code.md"]
    assert (dry["added_notes"], dry["updated_notes"], dry["deleted_notes"]) == (1, 1, 1)
    assert dry["dry_run"] is True
    assert fake_provider.embed_calls == []
    assert store.collection.count() == initial_count

    real = store.sync(str(tiny_vault))

    assert (real["added_notes"], real["updated_notes"], real["deleted_notes"]) == (1, 1, 1)
    assert real["dry_run"] is False


def test_dry_run_with_reset_is_refused(tmp_path, tiny_vault, fake_provider):
    """A dry run must never be destructive; reset would drop the collection."""
    store = build_store(tmp_path / "chroma", fake_provider)
    store.sync(str(tiny_vault))
    count = store.collection.count()

    with pytest.raises(ValueError):
        store.sync(str(tiny_vault), reset=True, dry_run=True)

    assert store.collection.count() == count


def test_embedding_failure_leaves_existing_index_intact(
    tmp_path, tiny_vault, fake_provider, monkeypatch
):
    store = build_store(tmp_path / "chroma", fake_provider)
    store.sync(str(tiny_vault))

    def snapshot():
        payload = store.collection.get(include=["documents", "metadatas"])
        documents = payload.get("documents") or []
        metadatas = payload.get("metadatas") or []
        return {
            entry_id: (document, metadata)
            for entry_id, document, metadata in zip(
                payload["ids"], documents, metadatas
            )
        }

    before = snapshot()
    (tiny_vault / "note_plain.md").write_text(
        "This update cannot be embedded right now.\n", encoding="utf-8"
    )

    def fail(*args, **kwargs):
        raise OpenRouterError("temporary provider failure")

    monkeypatch.setattr(fake_provider, "embed_texts", fail)

    with pytest.raises(OpenRouterError, match="temporary provider failure"):
        store.sync(str(tiny_vault))

    assert snapshot() == before
