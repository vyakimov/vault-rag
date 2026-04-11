import ctypes
import hashlib
import re
import string
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
from rich import box
from rich.console import Console
from rich.table import Table

pd.set_option("display.max_columns", None)
pd.set_option("display.width", None)
pd.set_option("display.max_colwidth", 20)
pd.set_option("display.precision", 3)
pd.set_option("display.expand_frame_repr", False)
pd.set_option("display.width", 0)

PRETTY_CONFIG = {"max_rows": 20, "col_width": 75, "show_index": False, "title": None}

WORD_RE = r"\b\w+\b"

# Small built-in list so the app does not depend on downloading NLTK corpora.
DEFAULT_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "he",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "that",
    "the",
    "to",
    "was",
    "were",
    "will",
    "with",
}

conversion_d = {
    idx: value for value, idx in zip(string.digits + string.ascii_letters, range(62))
}


def count_tokens(text: str, tokenizer: Any = None) -> int:
    """Count tokens, falling back to a lightweight approximation when needed."""
    if tokenizer is not None:
        token_ids = tokenizer.encode(text, add_special_tokens=True)
        return len(token_ids)
    return max(1, len(text) // 4)


def decimal_to_base(n: int, base: int = 62, conversion_table=conversion_d) -> str:
    if base > (max(conversion_table.keys()) + 1):
        conversion_table = None
    if n == 0:
        return "0"

    digits = []
    while n:
        digits.append(int(n % base))
        n //= base

    if conversion_table is not None:
        return "".join(conversion_table[x] for x in reversed(digits))
    return "".join(str(x) if x < 10 else chr(x + 55) for x in reversed(digits))


def hash_string(value: str) -> str:
    return decimal_to_base(
        ctypes.c_uint64(int(hashlib.md5(value.encode("utf-8")).hexdigest(), 16)).value
    )


def normalize_no_punct(text: str) -> str:
    return " ".join(re.findall(WORD_RE, text.lower()))


def tokenize_for_bm25(text: str, stop_words: Iterable[str], stemmer) -> List[str]:
    tokens = re.findall(WORD_RE, text.lower())
    return [stemmer.stem(token) for token in tokens if token and token not in stop_words]


class PrettyPrinter:
    def __init__(
        self,
        max_rows: Optional[int] = None,
        col_width: Optional[int] = None,
        show_index: bool = False,
        title: Optional[str] = None,
        float_precision: int = 3,
        console_width: int = 300,
        box_style: box.Box = box.SIMPLE,
        header_style: str = "bold cyan",
    ):
        self.max_rows = max_rows
        self.col_width = col_width
        self.show_index = show_index
        self.title = title
        self.float_precision = float_precision
        self.console_width = console_width
        self.box_style = box_style
        self.header_style = header_style

    def configure(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                raise ValueError(f"Unknown configuration option: {key}")
        return self

    def get_config(self) -> Dict[str, Any]:
        return {
            "max_rows": self.max_rows,
            "col_width": self.col_width,
            "show_index": self.show_index,
            "title": self.title,
            "float_precision": self.float_precision,
            "console_width": self.console_width,
            "box_style": self.box_style,
            "header_style": self.header_style,
        }

    @contextmanager
    def config(self, **kwargs):
        original = self.get_config()
        self.configure(**kwargs)
        try:
            yield self
        finally:
            self.configure(**original)

    def print(
        self,
        df: pd.DataFrame,
        max_rows: Optional[int] = None,
        col_width: Optional[int] = None,
        title: Optional[str] = None,
        show_index: Optional[bool] = None,
        float_precision: Optional[int] = None,
    ):
        max_rows = max_rows if max_rows is not None else self.max_rows
        col_width = col_width if col_width is not None else self.col_width
        title = title if title is not None else self.title
        show_index = show_index if show_index is not None else self.show_index
        float_precision = (
            float_precision if float_precision is not None else self.float_precision
        )

        console = Console(force_terminal=True, width=self.console_width)
        table = Table(
            title=title,
            box=self.box_style,
            show_header=True,
            header_style=self.header_style,
        )

        if show_index:
            table.add_column("", style="dim", width=6)

        for col in df.columns:
            col_name = str(col)
            if col_width and len(col_name) > col_width:
                col_name = col_name[: col_width - 3] + "..."
            table.add_column(col_name)

        rows = df.head(max_rows) if max_rows is not None else df
        for index, row in rows.iterrows():
            rendered = []
            if show_index:
                rendered.append(str(index))
            for value in row:
                if isinstance(value, float):
                    cell = f"{value:.{float_precision}f}"
                else:
                    cell = str(value)
                if col_width and len(cell) > col_width:
                    cell = cell[: col_width - 3] + "..."
                rendered.append(cell)
            table.add_row(*rendered)

        console.print(table)


printer = PrettyPrinter(**PRETTY_CONFIG)


def pretty_print(df: pd.DataFrame, **kwargs):
    printer.print(df, **kwargs)
