"""Read-only Chroma access for Vault RAG."""

from __future__ import annotations

from typing import Dict

import chromadb


class DatabaseReader:
    def __init__(
        self,
        chroma_db_path: str = "chroma_db",
        collection_name: str = "vault_notes",
    ):
        self.chroma_db_path = chroma_db_path
        self.collection_name = collection_name
        self.client = chromadb.PersistentClient(path=chroma_db_path)
        try:
            self.collection = self.client.get_collection(name=collection_name)
        except (ValueError, chromadb.errors.NotFoundError):
            self.collection = None

    def get_collection_stats(self) -> Dict[str, object]:
        if self.collection is None:
            return {"error": "Collection not found"}

        payload = self.collection.get(include=["metadatas"])
        metadatas = payload.get("metadatas") or []
        folders = {
            metadata.get("folder", "")
            for metadata in metadatas
            if metadata.get("folder")
        }
        tags = set()
        dated_notes = 0
        for metadata in metadatas:
            tag_string = metadata.get("tags", "")
            if tag_string:
                tags.update(tag.strip() for tag in tag_string.split(",") if tag.strip())
            if metadata.get("date"):
                dated_notes += 1

        return {
            "total_documents": self.collection.count(),
            "unique_folders": len(folders),
            "unique_tags": len(tags),
            "dated_notes": dated_notes,
        }
