"""Tests for vault_spider.corpus.identity."""

from __future__ import annotations

from vault_spider.corpus.identity import is_ulid, resolve_note_id
from vault_spider.utils import hash_string


class TestResolveNoteId:
    def test_frontmatter_id_wins(self):
        assert resolve_note_id({"id": "01HSZABCDEFGHJKMNPQRSTVWXY"}, "some/path.md") == (
            "01HSZABCDEFGHJKMNPQRSTVWXY"
        )

    def test_strips_whitespace(self):
        assert resolve_note_id({"id": "  abc  "}, "p.md") == "abc"

    def test_falls_back_to_path_hash(self):
        assert resolve_note_id({}, "some/path.md") == hash_string("some/path.md")

    def test_empty_id_falls_back(self):
        assert resolve_note_id({"id": "  "}, "p.md") == hash_string("p.md")

    def test_non_scalar_id_falls_back(self):
        assert resolve_note_id({"id": ["a", "b"]}, "p.md") == hash_string("p.md")


class TestIsUlid:
    def test_valid_ulid(self):
        assert is_ulid("01ARZ3NDEKTSV4RRFFQ69G5FAV")

    def test_wrong_length(self):
        assert not is_ulid("01ARZ3NDEK")

    def test_lowercase_rejected(self):
        assert not is_ulid("01arz3ndektsv4rrffq69g5fav")

    def test_non_string(self):
        assert not is_ulid(12345)
