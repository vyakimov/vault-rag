"""Assemble the retrieval output contract (the citation/evidence JSON)."""

from __future__ import annotations

from typing import Dict, List

import numpy as np

EXCERPT_CHARS = 700


def _zscores(values: List[float]) -> List[float]:
    if not values:
        return []
    arr = np.array(values, dtype=float)
    std = float(arr.std())
    if std < 1e-9:
        return [0.0] * len(values)
    mean = float(arr.mean())
    return ((arr - mean) / std).tolist()


def _why(row: Dict[str, object]) -> str:
    rerank_rank = row.get("rerank_rank")
    if rerank_rank is not None and int(rerank_rank) <= 3:
        return f"reranked into top {int(rerank_rank)} for this query"
    bm25_z = float(row.get("_bm25_z", 0.0))
    sem_z = float(row.get("_sem_z", 0.0))
    if bm25_z > sem_z:
        return "strong keyword match"
    if sem_z > bm25_z:
        return "strong semantic match"
    return "combined keyword+semantic signal"


def build_evidence(result_row: Dict[str, object]) -> Dict[str, object]:
    metadata: Dict[str, object] = result_row["metadata"]  # type: ignore[assignment]
    is_document = str(metadata.get("granularity", "document")) == "document"
    heading = "" if is_document else str(metadata.get("heading", ""))
    document = str(result_row["document"])
    reranker = result_row.get("reranker")
    return {
        "note_id": str(metadata.get("note_id", "")),
        "path": str(metadata.get("path", "")),
        "title": str(metadata.get("title", "")),
        "type": str(metadata.get("note_type", "")),
        "provenance": str(metadata.get("provenance", "")),
        "heading": heading,
        "chunk_id": str(result_row["id"]),
        "line_start": int(metadata.get("line_start", 0) or 0),
        "line_end": int(metadata.get("line_end", 0) or 0),
        "excerpt": document[:EXCERPT_CHARS],
        "scores": {
            "bm25": round(float(result_row["bm25"]), 4),
            "semantic": round(float(result_row["semantic"]), 4),
            "fused": round(float(result_row["fused"]), 4),
            "reranker": None if reranker is None else round(float(reranker), 4),
            "final": round(float(result_row["final"]), 4),
        },
        "why": _why(result_row),
    }


def build_retrieval_output(
    query: str,
    mode: str,
    granularity: str,
    rows: List[Dict[str, object]],
) -> Dict[str, object]:
    bm25_z = _zscores([float(row["bm25"]) for row in rows])
    sem_z = _zscores([float(row["semantic"]) for row in rows])
    for row, bz, sz in zip(rows, bm25_z, sem_z):
        row["_bm25_z"] = bz
        row["_sem_z"] = sz

    candidates = [build_evidence(row) for row in rows]
    return {
        "query": query,
        "mode": mode,
        "granularity": granularity,
        "candidates": candidates,
    }
