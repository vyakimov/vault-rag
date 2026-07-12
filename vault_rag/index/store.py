"""Build and maintain a Chroma collection for Markdown notes.

One note contributes one ``document``-granularity entry plus N ``section``
entries. Both live in a single collection, distinguished by the ``granularity``
metadata field.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Dict, List, Optional, Tuple

import chromadb
from nltk.stem import PorterStemmer
from rank_bm25 import BM25Okapi

from vault_rag.config import BM25_CONFIG
from vault_rag.corpus.chunker import document_text, section_text, split_sections
from vault_rag.corpus.loader import Note, load_notes
from vault_rag.llm.openrouter import OpenRouterClient
from vault_rag.utils import DEFAULT_STOP_WORDS, tokenize_for_bm25

GRANULARITIES = ("document", "section")


class IndexStore:
    """Maintain the persistent vector store and in-memory BM25 indexes."""

    def __init__(
        self,
        chroma_db_path: str = "chroma_db",
        collection_name: str = "vault_notes",
        bm25_k1: Optional[float] = None,
        bm25_b: Optional[float] = None,
        provider: Optional[OpenRouterClient] = None,
        allow_model_mismatch: bool = False,
    ):
        self.chroma_db_path = chroma_db_path
        self.collection_name = collection_name
        self.provider = provider or OpenRouterClient.from_env()
        # True when the caller intends to reset the collection anyway (sync
        # --reset); an embedding-model mismatch is then not an error.
        self.allow_model_mismatch = allow_model_mismatch
        self.client = chromadb.PersistentClient(path=chroma_db_path)
        self.bm25_k1 = BM25_CONFIG["k1"] if bm25_k1 is None else bm25_k1
        self.bm25_b = BM25_CONFIG["b"] if bm25_b is None else bm25_b
        self.stop_words = DEFAULT_STOP_WORDS
        self.stemmer = PorterStemmer()

        # Per-granularity in-memory state.
        self.documents: Dict[str, List[str]] = {g: [] for g in GRANULARITIES}
        self.ids: Dict[str, List[str]] = {g: [] for g in GRANULARITIES}
        self.metadatas: Dict[str, List[Dict[str, object]]] = {g: [] for g in GRANULARITIES}
        self.tokenized: Dict[str, List[List[str]]] = {g: [] for g in GRANULARITIES}
        self.bm25: Dict[str, Optional[BM25Okapi]] = {g: None for g in GRANULARITIES}

        self.collection = self._load_or_create_collection()
        self._rehydrate_from_collection()

    # -- collection lifecycle -------------------------------------------------

    def _collection_metadata(self) -> Dict[str, str]:
        return {
            "description": "Vault note embeddings",
            "provider": "openrouter",
            "embedding_model": self.provider.embedding_model,
        }

    def _load_or_create_collection(self):
        # The mismatch check must stay outside the try: raising it inside would
        # fall through to create_collection on an existing name and surface as
        # a confusing "collection already exists" error instead.
        try:
            collection = self.client.get_collection(name=self.collection_name)
        except (ValueError, chromadb.errors.NotFoundError):
            return self.client.create_collection(
                name=self.collection_name,
                metadata=self._collection_metadata(),
            )
        current_metadata = getattr(collection, "metadata", None) or {}
        existing_model = current_metadata.get("embedding_model")
        if (
            existing_model
            and existing_model != self.provider.embedding_model
            and not self.allow_model_mismatch
        ):
            raise ValueError(
                "Collection was built with a different embedding model. "
                "Run `vault-rag sync --reset` to rebuild it."
            )
        return collection

    def _reset_collection(self) -> None:
        try:
            self.client.delete_collection(self.collection_name)
        except (ValueError, chromadb.errors.NotFoundError):
            pass
        self.collection = self.client.create_collection(
            name=self.collection_name,
            metadata=self._collection_metadata(),
        )

    # -- in-memory rehydration ------------------------------------------------

    def _tokenize(self, documents: List[str]) -> List[List[str]]:
        return [
            tokenize_for_bm25(document, self.stop_words, self.stemmer)
            for document in documents
        ]

    def _rehydrate_from_collection(self) -> None:
        for granularity in GRANULARITIES:
            self.documents[granularity] = []
            self.ids[granularity] = []
            self.metadatas[granularity] = []
            self.tokenized[granularity] = []
            self.bm25[granularity] = None

        if self.collection.count() == 0:
            return

        payload = self.collection.get(include=["documents", "metadatas"])
        all_ids = payload.get("ids") or []
        all_documents = payload.get("documents") or []
        all_metadatas = payload.get("metadatas") or []

        for entry_id, document, metadata in zip(all_ids, all_documents, all_metadatas):
            granularity = str(metadata.get("granularity", "document"))
            if granularity not in self.documents:
                granularity = "document"
            self.ids[granularity].append(entry_id)
            self.documents[granularity].append(document)
            self.metadatas[granularity].append(metadata)

    def _ensure_bm25(self, granularity: str) -> None:
        if self.bm25[granularity] is not None or not self.documents[granularity]:
            return
        self.tokenized[granularity] = self._tokenize(self.documents[granularity])
        self.bm25[granularity] = BM25Okapi(
            self.tokenized[granularity], k1=self.bm25_k1, b=self.bm25_b
        )

    def granularity_data(
        self, granularity: str
    ) -> Tuple[List[str], List[str], List[Dict[str, object]], Optional[BM25Okapi]]:
        self._ensure_bm25(granularity)
        return (
            self.documents[granularity],
            self.ids[granularity],
            self.metadatas[granularity],
            self.bm25[granularity],
        )

    # -- entry assembly -------------------------------------------------------

    def _base_metadata(self, note: Note) -> Dict[str, object]:
        folder = PurePosixPath(note.path).parent.as_posix()
        return {
            "note_id": note.note_id,
            "title": note.title,
            "path": note.path,
            "folder": folder,
            "tags": ", ".join(note.tags),
            "date": note.date,
            "created": note.created or "",
            "updated": note.updated or "",
            "note_type": note.note_type,
            "content_hash": note.content_hash,
            "source": "vault_markdown",
        }

    def _entries_for_note(self, note: Note) -> List[Tuple[str, str, Dict[str, object]]]:
        entries: List[Tuple[str, str, Dict[str, object]]] = []

        doc_metadata = self._base_metadata(note)
        doc_metadata.update(
            {"granularity": "document", "heading": "", "line_start": 0, "line_end": 0}
        )
        entries.append((f"{note.note_id}::doc", document_text(note), doc_metadata))

        for section in split_sections(note):
            section_metadata = self._base_metadata(note)
            section_metadata.update(
                {
                    "granularity": "section",
                    "heading": section.heading,
                    "line_start": section.line_start,
                    "line_end": section.line_end,
                }
            )
            entries.append(
                (section.chunk_id, section_text(note, section), section_metadata)
            )
        return entries

    # -- sync -----------------------------------------------------------------

    def sync(
        self, root: str, reset: bool = False, dry_run: bool = False
    ) -> Dict[str, object]:
        if reset:
            self._reset_collection()

        notes = load_notes(root)
        warnings: List[str] = []

        # Two files sharing a frontmatter id would collide on entry ids; index
        # the first (load_notes is path-sorted) and skip the rest.
        seen_note_ids: Dict[str, str] = {}
        deduped: List[Note] = []
        for note in notes:
            first_path = seen_note_ids.get(note.note_id)
            if first_path is not None:
                warnings.append(
                    f"duplicate note id {note.note_id}: skipped {note.path} "
                    f"(already used by {first_path})"
                )
                continue
            seen_note_ids[note.note_id] = note.path
            deduped.append(note)
        notes = deduped

        existing = self.collection.get(include=["metadatas"])
        existing_ids = existing.get("ids") or []
        existing_metas = existing.get("metadatas") or []
        existing_by_note: Dict[str, Dict[str, object]] = {}
        for entry_id, metadata in zip(existing_ids, existing_metas):
            note_id = str(metadata.get("note_id", ""))
            group = existing_by_note.setdefault(
                note_id,
                {
                    "ids": [],
                    "content_hash": metadata.get("content_hash", ""),
                    "path": metadata.get("path", ""),
                },
            )
            group["ids"].append(entry_id)  # type: ignore[union-attr]

        ids_to_delete: List[str] = []
        entries_to_add: List[Tuple[str, str, Dict[str, object]]] = []
        would_add: List[str] = []
        would_update: List[str] = []
        would_delete: List[str] = []
        added_notes = updated_notes = deleted_notes = unchanged = 0

        disk_note_ids = set()
        for note in notes:
            disk_note_ids.add(note.note_id)
            group = existing_by_note.get(note.note_id)
            if group is None:
                entries_to_add.extend(self._entries_for_note(note))
                would_add.append(note.path)
                added_notes += 1
            elif (
                group.get("content_hash") != note.content_hash
                or group.get("path") != note.path
            ):
                # Path change with identical content (a moved note) must also
                # re-index, or path/folder/title metadata goes stale.
                ids_to_delete.extend(group["ids"])  # type: ignore[arg-type]
                entries_to_add.extend(self._entries_for_note(note))
                would_update.append(note.path)
                updated_notes += 1
            else:
                unchanged += 1

        for note_id, group in existing_by_note.items():
            if note_id not in disk_note_ids:
                ids_to_delete.extend(group["ids"])  # type: ignore[arg-type]
                would_delete.append(str(group.get("path") or note_id))
                deleted_notes += 1

        if dry_run:
            return {
                "added_notes": added_notes,
                "updated_notes": updated_notes,
                "deleted_notes": deleted_notes,
                "unchanged": unchanged,
                "total_entries": self.collection.count(),
                "warnings": warnings,
                "dry_run": True,
                "would_add": sorted(would_add),
                "would_update": sorted(would_update),
                "would_delete": sorted(would_delete),
            }

        if ids_to_delete:
            self.collection.delete(ids=ids_to_delete)

        if entries_to_add:
            add_ids = [entry[0] for entry in entries_to_add]
            add_texts = [entry[1] for entry in entries_to_add]
            add_metas = [entry[2] for entry in entries_to_add]
            embeddings = self.provider.embed_texts(add_texts, batch_size=32)
            self._add_in_batches(add_ids, add_texts, add_metas, embeddings)

        self._rehydrate_from_collection()

        return {
            "added_notes": added_notes,
            "updated_notes": updated_notes,
            "deleted_notes": deleted_notes,
            "unchanged": unchanged,
            "total_entries": self.collection.count(),
            "warnings": warnings,
            "dry_run": False,
        }

    def _add_in_batches(
        self,
        ids: List[str],
        documents: List[str],
        metadatas: List[Dict[str, object]],
        embeddings: List[List[float]],
        batch_size: int = 512,
    ) -> None:
        for start in range(0, len(ids), batch_size):
            end = start + batch_size
            self.collection.add(
                ids=ids[start:end],
                documents=documents[start:end],
                metadatas=metadatas[start:end],
                embeddings=embeddings[start:end],
            )

    # -- stats ----------------------------------------------------------------

    def get_collection_stats(self) -> Dict[str, object]:
        document_metas = self.metadatas["document"]
        if not document_metas:
            return {"total_documents": 0, "total_entries": self.collection.count()}

        folders = set()
        tag_values = set()
        dated_notes = 0
        for metadata in document_metas:
            folder = metadata.get("folder")
            if folder:
                folders.add(folder)
            tags = metadata.get("tags")
            if tags:
                tag_values.update(
                    tag.strip() for tag in str(tags).split(",") if tag.strip()
                )
            if metadata.get("date"):
                dated_notes += 1

        return {
            "total_documents": len(document_metas),
            "total_entries": self.collection.count(),
            "section_entries": len(self.metadatas["section"]),
            "unique_folders": len(folders),
            "unique_tags": len(tag_values),
            "dated_notes": dated_notes,
            "embedding_model": self.provider.embedding_model,
        }
