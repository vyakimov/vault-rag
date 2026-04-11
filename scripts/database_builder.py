"""Build and maintain a Chroma collection for Markdown notes."""

from __future__ import annotations

from typing import Dict, List, Optional

import chromadb
from nltk.stem import PorterStemmer
from rank_bm25 import BM25Okapi

try:
    from config import BM25_CONFIG
    from openrouter_client import OpenRouterClient
    from utils import DEFAULT_STOP_WORDS, tokenize_for_bm25
except ImportError:
    from scripts.config import BM25_CONFIG
    from scripts.openrouter_client import OpenRouterClient
    from scripts.utils import DEFAULT_STOP_WORDS, tokenize_for_bm25


class DatabaseBuilder:
    """Maintain the persistent vector store and in-memory BM25 index."""

    def __init__(
        self,
        chroma_db_path: str = "chroma_db",
        collection_name: str = "vault_notes",
        bm25_k1: Optional[float] = None,
        bm25_b: Optional[float] = None,
        provider: Optional[OpenRouterClient] = None,
    ):
        self.chroma_db_path = chroma_db_path
        self.collection_name = collection_name
        self.provider = provider or OpenRouterClient.from_env()
        self.client = chromadb.PersistentClient(path=chroma_db_path)
        self.bm25_k1 = BM25_CONFIG["k1"] if bm25_k1 is None else bm25_k1
        self.bm25_b = BM25_CONFIG["b"] if bm25_b is None else bm25_b
        self.stop_words = DEFAULT_STOP_WORDS
        self.stemmer = PorterStemmer()

        self.documents: List[str] = []
        self.document_ids: List[str] = []
        self.metadatas: List[Dict[str, str]] = []
        self.tokenized_documents: List[List[str]] = []
        self.bm25: Optional[BM25Okapi] = None

        self.collection = self._load_or_create_collection()
        self._rehydrate_from_collection()

    def _load_or_create_collection(self):
        expected_metadata = {
            "description": "Vault note embeddings",
            "provider": "openrouter",
            "embedding_model": self.provider.embedding_model,
        }

        try:
            collection = self.client.get_collection(name=self.collection_name)
            current_metadata = getattr(collection, "metadata", None) or {}
            existing_model = current_metadata.get("embedding_model")
            if existing_model and existing_model != self.provider.embedding_model:
                raise ValueError(
                    "Collection was built with a different embedding model. "
                    "Reset the collection before rebuilding."
                )
            return collection
        except (ValueError, chromadb.errors.NotFoundError):
            return self.client.create_collection(
                name=self.collection_name,
                metadata=expected_metadata,
            )

    def _rehydrate_from_collection(self) -> None:
        count = self.collection.count()
        if count == 0:
            self.bm25 = None
            return

        payload = self.collection.get(include=["documents", "metadatas"])
        self.documents = payload.get("documents") or []
        self.document_ids = payload.get("ids") or []
        self.metadatas = payload.get("metadatas") or []
        self.tokenized_documents = self._tokenize_documents(self.documents)
        self.bm25 = BM25Okapi(
            self.tokenized_documents, k1=self.bm25_k1, b=self.bm25_b
        )

    def _tokenize_documents(self, documents: List[str]) -> List[List[str]]:
        return [
            tokenize_for_bm25(document, self.stop_words, self.stemmer)
            for document in documents
        ]

    def add_documents_to_collection(
        self,
        note_documents: List[Dict[str, str]],
        batch_size: int = 32,
    ) -> int:
        existing_ids = set(self.document_ids)
        new_documents = [
            note_document
            for note_document in note_documents
            if note_document["id"] not in existing_ids
        ]
        if not new_documents:
            return 0

        new_ids = [note_document["id"] for note_document in new_documents]
        new_texts = [note_document["document"] for note_document in new_documents]
        new_metadata = [note_document["metadata"] for note_document in new_documents]

        self.documents.extend(new_texts)
        self.document_ids.extend(new_ids)
        self.metadatas.extend(new_metadata)
        self.tokenized_documents.extend(self._tokenize_documents(new_texts))
        self.bm25 = BM25Okapi(self.tokenized_documents, k1=self.bm25_k1, b=self.bm25_b)

        embeddings = self.provider.embed_texts(new_texts, batch_size=batch_size)
        self.collection.add(
            ids=new_ids,
            documents=new_texts,
            metadatas=new_metadata,
            embeddings=embeddings,
        )
        return len(new_documents)

    def get_collection_stats(self) -> Dict[str, object]:
        count = self.collection.count()
        if count == 0:
            return {"total_documents": 0}

        folders = set()
        tag_values = set()
        dated_notes = 0
        for metadata in self.metadatas:
            folder = metadata.get("folder")
            if folder:
                folders.add(folder)
            tags = metadata.get("tags")
            if tags:
                tag_values.update(tag.strip() for tag in tags.split(",") if tag.strip())
            if metadata.get("date"):
                dated_notes += 1

        return {
            "total_documents": count,
            "unique_folders": len(folders),
            "unique_tags": len(tag_values),
            "dated_notes": dated_notes,
            "embedding_model": self.provider.embedding_model,
        }
