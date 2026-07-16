"""Deterministic section splitting for notes (no LLM)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

from vault_spider.corpus.loader import Note

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass
class Section:
    chunk_id: str        # f"{note_id}::s{index:03d}"
    note_id: str
    heading: str         # nearest heading text ("" for preamble)
    level: int           # heading level, 0 for preamble
    line_start: int      # 1-based, inclusive, within the BODY
    line_end: int        # 1-based, inclusive
    text: str


def _segment_lines(lines: List[str]) -> List[Tuple[str, int, int, int]]:
    """Split body lines into (heading, level, start0, end0) segments.

    Segments break on level 1-3 headings found outside fenced code blocks.
    Level 4-6 headings stay inside their parent. Content before the first
    heading is a level-0 preamble segment.
    """
    in_fence = False
    starts: List[Tuple[int, str, int]] = []  # (line_index_0based, heading, level)
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            if 1 <= level <= 3:
                starts.append((index, match.group(2).strip(), level))

    n = len(lines)
    segments: List[Tuple[str, int, int, int]] = []
    if not starts:
        segments.append(("", 0, 0, n - 1))
        return segments

    first_start = starts[0][0]
    if first_start > 0:
        segments.append(("", 0, 0, first_start - 1))
    for position, (index, heading, level) in enumerate(starts):
        end = starts[position + 1][0] - 1 if position + 1 < len(starts) else n - 1
        segments.append((heading, level, index, end))
    return segments


def _windows(
    seg_lines: List[str],
    max_chars: int,
    overlap_chars: int,
) -> List[Tuple[int, int]]:
    """Break a segment's lines into (start_local, end_local) windows.

    Windows never break mid-line, each stays within ``max_chars`` (except a
    single oversize line), and consecutive windows overlap by ~``overlap_chars``.
    """
    m = len(seg_lines)
    if m == 0:
        return [(0, -1)]

    windows: List[Tuple[int, int]] = []
    start = 0
    while True:
        cur_len = 0
        end = start
        j = start
        while j < m:
            add = len(seg_lines[j]) + (1 if j > start else 0)
            if j > start and cur_len + add > max_chars:
                break
            cur_len += add
            end = j
            j += 1
        windows.append((start, end))
        if end >= m - 1:
            break

        overlap_len = 0
        k = end
        while k > start and overlap_len + len(seg_lines[k]) + 1 <= overlap_chars:
            overlap_len += len(seg_lines[k]) + 1
            k -= 1
        next_start = k + 1
        if next_start <= start:
            next_start = start + 1
        start = next_start
    return windows


def split_sections(note: Note, max_chars: int = 6000, overlap_chars: int = 300) -> List[Section]:
    lines = note.body.split("\n")
    segments = _segment_lines(lines)

    sections: List[Section] = []
    for heading, level, start0, end0 in segments:
        seg_lines = lines[start0 : end0 + 1]
        # A level-0 (preamble / whole-body) segment is only emitted if non-blank.
        if level == 0 and not "\n".join(seg_lines).strip():
            continue
        for win_start, win_end in _windows(seg_lines, max_chars, overlap_chars):
            body_start0 = start0 + win_start
            body_end0 = start0 + win_end
            text = "\n".join(lines[body_start0 : body_end0 + 1])
            index = len(sections)
            sections.append(
                Section(
                    chunk_id=f"{note.note_id}::s{index:03d}",
                    note_id=note.note_id,
                    heading=heading,
                    level=level,
                    line_start=body_start0 + 1,
                    line_end=body_end0 + 1,
                    text=text,
                )
            )
    return sections


def document_text(note: Note) -> str:
    parts = [f"# {note.title}", f"Path: {note.path}"]
    if note.tags:
        parts.append(f"Tags: {', '.join(note.tags)}")
    if note.date:
        parts.append(f"Date: {note.date}")
    if note.body.strip():
        parts.append(note.body.strip())
    return "\n\n".join(parts).strip()


def section_text(note: Note, section: Section) -> str:
    return f"# {note.title}\n\nSection: {section.heading or '(intro)'}\n\n{section.text}"
