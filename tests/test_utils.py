"""Tests for pure helpers in vault_spider.utils."""

from __future__ import annotations

import pytest
from nltk.stem import PorterStemmer

from vault_spider.utils import (
    DEFAULT_STOP_WORDS,
    decimal_to_base,
    hash_string,
    normalize_no_punct,
    tokenize_for_bm25,
    validate_vault_relative_path,
)


class TestDecimalToBase:
    def test_zero(self):
        assert decimal_to_base(0) == "0"

    def test_base62_roundtrip_is_alphanumeric(self):
        result = decimal_to_base(123456789)
        assert result.isalnum()

    def test_distinct_inputs_give_distinct_outputs(self):
        assert decimal_to_base(1) != decimal_to_base(2)


class TestHashString:
    def test_golden_note_identity(self):
        # Frozen: note identity depends on this exact output.
        assert hash_string("folder/note.md") == "aS6BvOeyuVS"

    def test_deterministic(self):
        assert hash_string("hello") == hash_string("hello")

    def test_different_inputs_produce_different_hashes(self):
        assert hash_string("a") != hash_string("b")

    def test_returns_non_empty_string(self):
        assert hash_string("anything")


class TestNormalizeNoPunct:
    def test_lowercases_and_strips_punctuation(self):
        assert normalize_no_punct("Hello, World!") == "hello world"

    def test_collapses_whitespace(self):
        assert normalize_no_punct("a  b\tc\nd") == "a b c d"

    def test_empty(self):
        assert normalize_no_punct("") == ""


class TestTokenizeForBm25:
    def test_removes_stop_words_and_stems(self):
        stemmer = PorterStemmer()
        tokens = tokenize_for_bm25(
            "The running dogs are jumping.", DEFAULT_STOP_WORDS, stemmer
        )
        assert "the" not in tokens
        assert "are" not in tokens
        assert "run" in tokens
        assert "jump" in tokens

    def test_lowercases(self):
        stemmer = PorterStemmer()
        tokens = tokenize_for_bm25("Dogs", set(), stemmer)
        assert tokens == [stemmer.stem("dogs")]


class TestValidateVaultRelativePath:
    def test_accepts_canonical_nested_path(self):
        assert validate_vault_relative_path("Research/Alpha.md") == "Research/Alpha.md"

    @pytest.mark.parametrize("path", ["/absolute.md", "../escape.md", "a/./b.md", "a\\b.md"])
    def test_rejects_ambiguous_or_escaping_paths(self, path):
        with pytest.raises(ValueError):
            validate_vault_relative_path(path)
