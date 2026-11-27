"""Single source of truth for the product version.

Every place that needs a version — window title, About section,
QApplication's applicationVersion, installer naming, future update checks —
reads from this one file so the number never drifts between modules.

This is a pure-data module with no Qt or third-party imports, which means
packaging scripts and command-line tools can read it safely too.

To release a new version: bump the three integers below, update CODENAME and
RELEASE_DATE, and prepend one entry to RELEASES. Nothing else needs editing.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Current version — semantic versioning (major.minor.patch)
# ---------------------------------------------------------------------------
MAJOR = 1
MINOR = 0
PATCH = 0

# Codename, echoing the default "暖白晨光" theme so build and UI share a voice.
CODENAME = "晨光"

# ISO 8601 release date.
RELEASE_DATE = "2026-11-27"

VERSION = f"{MAJOR}.{MINOR}.{PATCH}"
VERSION_TUPLE = (MAJOR, MINOR, PATCH)

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def version_string(prefix: str = "v") -> str:
    """e.g. ``v1.0.0``."""
    return f"{prefix}{VERSION}"


def display_version() -> str:
    """Human-facing form used in the UI, e.g. ``v1.0.0 · 晨光``."""
    return f"{version_string()} · {CODENAME}"


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Parse ``1.2.3`` into ``(1, 2, 3)``; return None for malformed input."""
    match = _VERSION_RE.match(str(text).strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_newer_than(other: str) -> bool:
    """Whether the current version is newer than ``other`` (e.g. ``0.9.2``).

    Unparseable input is treated as "not newer" so a bad stored value can
    never trigger a false update prompt.
    """
    parsed = parse_version(other)
    return parsed is not None and VERSION_TUPLE > parsed


# ---------------------------------------------------------------------------
# Release history — newest first. Prepend one dict per release.
# ---------------------------------------------------------------------------
RELEASES: tuple[dict[str, object], ...] = (
    {
        "version": VERSION,
        "codename": CODENAME,
        "date": RELEASE_DATE,
        "notes": (
            "四套可即时切换的主题（暖白晨光 / 墨蓝夜曲 / 奶油胶片 / 薄雾清晨）",
            "无按钮、无系统音效的绿色提醒浮层",
            "主题感知的图标、封面与背景洗白层",
        ),
    },
)
