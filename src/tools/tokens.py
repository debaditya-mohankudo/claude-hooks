"""Token-count helper — approximates Claude's tokenizer, which isn't public.

Uses tiktoken's cl100k_base encoding (GPT-3.5/4 family) as the closest
practical stand-in. Not exact for Claude models, but far tighter than the
chars/4 heuristic used elsewhere in this repo before this existed.
"""
from __future__ import annotations

import tiktoken

_ENCODING = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Token count for `text` under cl100k_base. Empty/falsy input -> 0."""
    if not text:
        return 0
    return len(_ENCODING.encode(text))
