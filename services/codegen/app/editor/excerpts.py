"""Shared, line-safe excerpts for editor and sandbox failure output."""

from __future__ import annotations


DEFAULT_ERROR_TAIL_CHARS = 800


def tail_excerpt(text: str, *, limit: int = DEFAULT_ERROR_TAIL_CHARS) -> str:
    """Return a bounded tail that starts on a complete line.

    A raw ``text[-limit:]`` slice can begin in the middle of an import path or
    exception line and make intact output look corrupted. This helper drops
    that partial leading line and explicitly reports how much was omitted.
    """
    if limit < 1:
        raise ValueError("tail excerpt limit must be positive")
    text = text.strip()
    if len(text) <= limit:
        return text
    slice_start = len(text) - limit
    clipped = text[slice_start:]
    if text[slice_start - 1] != "\n":
        newline = clipped.find("\n")
        if newline < 0:
            return (
                f"[…truncated all {len(text)} chars; final line exceeds the "
                f"{limit}-char excerpt limit…]"
            )
        clipped = clipped[newline + 1 :]
    dropped = len(text) - len(clipped)
    return f"[…truncated {dropped} leading chars of {len(text)}…]\n{clipped}"
