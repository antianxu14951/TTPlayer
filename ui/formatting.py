"""Formatting helpers shared by the library and queue tables."""

from __future__ import annotations


def format_duration(milliseconds: int) -> str:
    """mm:ss for any millisecond value, clamped at zero."""
    seconds = max(0, int(milliseconds // 1000))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
