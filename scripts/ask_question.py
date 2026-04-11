import argparse
import os
import sys

try:
    from scripts.database_builder import DatabaseBuilder
    from scripts.searcher import Searcher
except ImportError:
    try:
        from database_builder import DatabaseBuilder
        from searcher import Searcher
    except ImportError:
        script_dir = os.path.join(os.getcwd(), "scripts")
        sys.path.insert(0, script_dir)
        from .database_builder import DatabaseBuilder  # pyright: ignore
        from .searcher import Searcher  # pyright: ignore


def ask_question(question: str, output_file: str | None = None):
    print("=== Initializing Vault Database Connection ===")
    builder = DatabaseBuilder()
    current_count = builder.collection.count()
    if current_count == 0:
        print("Error: No notes found in the database.")
        print("Please run `uv run scripts/build_database.py` first.")
        return

    searcher = Searcher(
        collection=builder.collection,
        documents=builder.documents,
        document_ids=builder.document_ids,
        metadatas=builder.metadatas,
        bm25=builder.bm25,
        tokenized_documents=builder.tokenized_documents,
        provider=builder.provider,
    )
    results = searcher.hybrid_search(question)

    def _at(seq, index):
        return float(seq.iloc[index]) if hasattr(seq, "iloc") else float(seq[index])

    output_lines = [f"# Query\n> {question}\n", "# Results"]
    for index, document in enumerate(results["documents"]):
        metadata = results.get("metadatas", [{}])[index]
        output_lines.append(f"\n## Note {index + 1}")
        output_lines.append(f"Title: {metadata.get('title', '(untitled)')}")
        output_lines.append(f"Path: {metadata.get('path', '(unknown)')}")
        if metadata.get("date"):
            output_lines.append(f"Date: {metadata['date']}")
        output_lines.append(document)
        output_lines.append("")
        output_lines.append(f"Final Score: {_at(results['boosted_scores'], index):.4f}")
        output_lines.append(f"Fused Score: {_at(results['fused_scores'], index):.4f}")
        if results.get("reranked_scores"):
            output_lines.append(
                f"Reranked Score: {_at(results['reranked_scores'], index):.4f}"
            )
        output_lines.append(
            f"Semantic Score: {_at(results['semantic_scores'], index):.4f}"
        )
        output_lines.append(
            f"Keyword Score: {_at(results['keyword_scores'], index):.4f}"
        )

    output_text = "\n".join(output_lines)
    if output_file:
        with open(output_file, "w", encoding="utf-8") as handle:
            handle.write(output_text)
        print(f"Results written to {output_file}")
    else:
        print(output_text)


def main():
    parser = argparse.ArgumentParser(description="Ask a question against the vault")
    parser.add_argument("question", help="The question to ask")
    parser.add_argument("-o", "--output", help="Optional output file")
    args = parser.parse_args()

    if not args.question.strip():
        print("Error: Please provide a non-empty question.")
        sys.exit(1)

    ask_question(args.question, args.output)


if __name__ == "__main__":
    main()
