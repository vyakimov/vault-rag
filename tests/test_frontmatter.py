"""Tests for vault_spider.corpus.frontmatter (parsing + normalization)."""

from __future__ import annotations

import datetime as dt

from vault_spider.corpus.frontmatter import (
    coerce_datetime,
    normalize_tags,
    split_frontmatter,
)


class TestSplitFrontmatter:
    def test_parses_yaml_frontmatter(self):
        raw = "---\ntitle: Hello\ntags: [a, b]\n---\nbody text\n"
        fm, body = split_frontmatter(raw)
        assert fm == {"title": "Hello", "tags": ["a", "b"]}
        assert body.strip() == "body text"

    def test_returns_empty_when_no_frontmatter(self):
        raw = "just body\n"
        fm, body = split_frontmatter(raw)
        assert fm == {}
        assert body == raw

    def test_returns_raw_on_invalid_yaml(self):
        raw = "---\ntitle: : :\n  bad\n---\nbody\n"
        fm, body = split_frontmatter(raw)
        assert fm == {}
        assert body == raw


class TestNormalizeTags:
    def test_list_input(self):
        assert normalize_tags(["a", "b", " c "]) == ["a", "b", "c"]

    def test_comma_separated_string(self):
        assert normalize_tags("a, b, c") == ["a", "b", "c"]

    def test_space_separated_string(self):
        assert normalize_tags("a b c") == ["a", "b", "c"]

    def test_none_returns_empty(self):
        assert normalize_tags(None) == []


class TestCoerceDatetime:
    def test_naive_string_treated_as_local(self):
        # A naive frontmatter timestamp is local wall-clock: the offset is attached
        # but the wall-clock time is preserved (never shifted as if it were UTC).
        result = coerce_datetime("2024-05-01T15:10")
        assert result is not None
        assert result.tzinfo is not None
        assert result == dt.datetime(2024, 5, 1, 15, 10).astimezone()
        assert (result.hour, result.minute) == (15, 10)

    def test_date_only_string_local_midnight(self):
        result = coerce_datetime("2024-05-01")
        assert result == dt.datetime(2024, 5, 1).astimezone()

    def test_date_object(self):
        result = coerce_datetime(dt.date(2024, 5, 1))
        assert result == dt.datetime(2024, 5, 1).astimezone()

    def test_invalid_string_returns_none(self):
        assert coerce_datetime("not a date") is None

    def test_none_returns_none(self):
        assert coerce_datetime(None) is None
