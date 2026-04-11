#!/usr/bin/env python
"""Parameter tuning helper for Vault RAG."""

import argparse
import os
import sys
from typing import List

from rank_bm25 import BM25Okapi

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)

from database_builder import DatabaseBuilder
from searcher import Searcher


def test_parameters(
    queries: List[str],
    k1_values: List[float],
    b_values: List[float],
    semantic_weights: List[float],
    n_results: int = 5,
) -> None:
    print("=" * 80)
    print("PARAMETER TUNING FOR VAULT RAG")
    print("=" * 80)

    db = DatabaseBuilder()
    if db.collection.count() == 0:
        print("Error: No notes in database. Run build_database.py first.")
        return

    for k1 in k1_values:
        for b in b_values:
            print(f"\n{'=' * 60}")
            print(f"Testing BM25 with k1={k1}, b={b}")
            print(f"{'=' * 60}")
            bm25 = BM25Okapi(db.tokenized_documents, k1=k1, b=b)

            for semantic_weight in semantic_weights:
                print(f"\n--- Semantic weight: {semantic_weight} ---")
                searcher = Searcher(
                    collection=db.collection,
                    documents=db.documents,
                    document_ids=db.document_ids,
                    metadatas=db.metadatas,
                    bm25=bm25,
                    tokenized_documents=db.tokenized_documents,
                    provider=db.provider,
                )

                for query in queries:
                    print(f"\nQuery: '{query}'")
                    results = searcher.hybrid_search(
                        query=query,
                        number_of_results=n_results,
                        semantic_weight=semantic_weight,
                    )
                    for index, score in enumerate(results["boosted_scores"]):
                        preview = results["documents"][index][:100].replace("\n", " ")
                        print(f"  {index + 1}. Score: {score:.4f} | {preview}...")


def main():
    parser = argparse.ArgumentParser(description="Tune BM25 and hybrid search parameters")
    parser.add_argument(
        "-q",
        "--queries",
        nargs="+",
        default=[
            "OpenClaw VPS migration",
            "daily note about work tasks",
            "Docker setup notes",
        ],
        help="Queries to test",
    )
    parser.add_argument("--k1", nargs="+", type=float, default=[0.9, 1.2, 1.5])
    parser.add_argument("--b", nargs="+", type=float, default=[0.5, 0.75])
    parser.add_argument(
        "--sw",
        "--semantic-weight",
        nargs="+",
        type=float,
        default=[0.4, 0.5, 0.6],
        dest="semantic_weights",
    )
    parser.add_argument("-n", "--n-results", type=int, default=3)
    args = parser.parse_args()

    test_parameters(
        queries=args.queries,
        k1_values=args.k1,
        b_values=args.b,
        semantic_weights=args.semantic_weights,
        n_results=args.n_results,
    )


if __name__ == "__main__":
    main()
