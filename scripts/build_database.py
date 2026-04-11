from __future__ import annotations

try:
    from database_builder import DatabaseBuilder
    from vault_ingestion import load_markdown_notes
except ImportError:
    from scripts.database_builder import DatabaseBuilder
    from scripts.vault_ingestion import load_markdown_notes


def build_database(vault_path: str = "./input/Vault 14") -> DatabaseBuilder:
    builder = DatabaseBuilder()

    current_count = builder.collection.count()
    if current_count > 0:
        print(f"Collection already contains {current_count} notes.")
        choice = input(
            "Do you want to (a)dd more notes, (r)eset and reload all, or (s)kip loading? [a/r/s]: "
        ).lower()
        if choice == "r":
            builder.client.delete_collection(builder.collection_name)
            builder.collection = builder.client.create_collection(
                name=builder.collection_name,
                metadata={
                    "description": "Vault note embeddings",
                    "provider": "openrouter",
                    "embedding_model": builder.provider.embedding_model,
                },
            )
            builder.documents = []
            builder.document_ids = []
            builder.metadatas = []
            builder.tokenized_documents = []
            print("Collection reset.")
        elif choice == "s":
            print("Skipping note loading.")
            return builder
        elif choice != "a":
            raise ValueError("Invalid choice")

    note_documents = load_markdown_notes(vault_path)
    added_count = builder.add_documents_to_collection(note_documents)
    print(f"Added {added_count} notes.")

    print("\nCollection Statistics:")
    for key, value in builder.get_collection_stats().items():
        print(f"  {key}: {value}")
    return builder


def main():
    print("\n=== Building Vault Database ===")
    build_database()
    print("\n=== Build Complete ===")
    print('Use `uv run scripts/ask_question.py "your question"` to search the vault.')


if __name__ == "__main__":
    main()
