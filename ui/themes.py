"""Switchable colour themes.

Every theme is built from the same brand triad — cream/ink/vermilion — and
differs only in lightness and temperature. The accent stays vermilion in all
themes so "red means play / active" never changes meaning; only the ground
and the supporting tone move. This keeps the three-colour rule intact no
matter which theme is selected.

QSS has no variables, so a tokenised template is rendered per theme. The
tokens were extracted from the original stylesheet and are verified to
reproduce it byte-for-byte for the default theme (see the module tests).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# ---------------------------------------------------------------------------
# Theme catalogue
# ---------------------------------------------------------------------------

THEME_DAYLIGHT = "daylight"
THEME_MIDNIGHT = "midnight"
THEME_CREAMY = "creamy"
THEME_MIST = "mist"


@dataclass(frozen=True)
class ThemeInfo:
    id: str
    name: str
    description: str
    # 三个色相的角色说明，用于设置界面里的三色预览点
    swatches: tuple[str, str, str]


THEMES: dict[str, ThemeInfo] = {
    THEME_DAYLIGHT: ThemeInfo(
        THEME_DAYLIGHT, "暖白晨光", "象牙白 · 墨蓝黑 · 朱红",
        ("#F7F4EE", "#1E2333", "#F5513A"),
    ),
    THEME_MIDNIGHT: ThemeInfo(
        THEME_MIDNIGHT, "墨蓝夜曲", "深墨蓝 · 暖银灰 · 朱红",
        ("#171B29", "#E8E4DA", "#F5513A"),
    ),
    THEME_CREAMY: ThemeInfo(
        THEME_CREAMY, "奶油胶片", "羊皮纸黄 · 深可可 · 朱红",
        ("#F1E5CD", "#33281A", "#F5513A"),
    ),
    THEME_MIST: ThemeInfo(
        THEME_MIST, "薄雾清晨", "冷雾白 · 石板蓝 · 朱红",
        ("#F4F6F8", "#2E3A4B", "#F5513A"),
    ),
}
DEFAULT_THEME = THEME_DAYLIGHT


# ---------------------------------------------------------------------------
# Token template
# ---------------------------------------------------------------------------

# 原色值 -> token 名。#ffffff 有两种语义，单独处理。
TOKEN_MAP: dict[str, str] = {
    "#F7F4EE": "BG",
    "#EAE6DE": "BORDER",
    "#F0EDE6": "BORDER_SOFT",
    "#F2EFE8": "BORDER_ROW",
    "#E4E0D7": "BORDER_INPUT",
    "#1E2333": "TEXT",
    "#939AA9": "MUTED",
    "#8A91A0": "MUTED2",
    "#6B7285": "SUBTLE",
    "#5C6475": "ICON",
    "#596073": "SECONDARY",
    "#3A4255": "BODY",
    "#A8AFBA": "FAINT",
    "#C4C6CC": "DISABLED",
    "#343a52": "PRESSED_TEXT",
    "#b9bce4": "FOCUS_HOVER",
    "#c5c8df": "FOCUS_PRESS",
    "#e8eaf6": "PRESS_BG",
    "#dde1ec": "SPLITTER_HOVER",
    "#F5513A": "ACCENT",
    "#FF6B4A": "ACCENT_HOVER",
    "#D8402C": "ACCENT_DEEP",
    "#C23824": "ACCENT_PRESS",
    "#E34A31": "ACCENT_HOVER_DEEP",
    "#FF8062": "ACCENT_HOVER_LIGHT",
    "#F4A99C": "ACCENT_DISABLED",
    "#FDEDE8": "TINT",
    "#FBE0D9": "TINT_HOVER",
    "#FBF9F5": "SURFACE_ALT",
    "#FDFCF9": "SURFACE_STRIPE",
    "#F5F1E9": "SURFACE_HOVER",
    "#CAC7BC": "SCROLL",
}

ALL_TOKENS = set(TOKEN_MAP.values()) | {"SURFACE", "ON_ACCENT"}


def _tokenize(sheet: str) -> str:
    """把一个 QSS 模板的字符串里的色值换成 @@TOKEN@@ 占位符。"""
    result = sheet.replace("color: #ffffff;", "color: @@ON_ACCENT@@;")
    result = result.replace("#ffffff", "@@SURFACE@@")
    for old, token in TOKEN_MAP.items():
        result = result.replace(old, f"@@{token}@@")
    return result


def _render(template: str, palette: dict[str, str]) -> str:
    for token, value in palette.items():
        template = template.replace(f"@@{token}@@", value)
    return template


# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

# 暖白晨光 —— 现有主题，逐 token 用原值，保证像素级不变。
_DAYLIGHT: dict[str, str] = {name: old for old, name in TOKEN_MAP.items()}
_DAYLIGHT["SURFACE"] = "#ffffff"
_DAYLIGHT["ON_ACCENT"] = "#ffffff"

# 所有主题共享朱红点缀及其渐变色阶；只变地面/辅助/文字。
_SHARED_ACCENT: dict[str, str] = {
    "ACCENT": "#F5513A",
    "ACCENT_HOVER": "#FF6B4A",
    "ACCENT_DEEP": "#D8402C",
    "ACCENT_PRESS": "#C23824",
    "ACCENT_HOVER_DEEP": "#E34A31",
    "ACCENT_HOVER_LIGHT": "#FF8062",
    "ACCENT_DISABLED": "#F4A99C",
    "ON_ACCENT": "#ffffff",
}


def _palette(**tokens: str) -> dict[str, str]:
    """构造调色板：共享朱红点缀 + 传入的主/辅/地面 token。"""
    palette = dict(_SHARED_ACCENT)
    palette.update(tokens)
    missing = ALL_TOKENS - set(palette)
    if missing:
        raise ValueError(f"主题调色板缺少 token: {sorted(missing)}")
    return palette


_MIDNIGHT = _palette(
    BG="#10131E",
    SURFACE="#1B2030",
    SURFACE_ALT="#212739",
    SURFACE_STRIPE="#1E2434",
    SURFACE_HOVER="#2A3144",
    BORDER="#2C3448",
    BORDER_SOFT="#262D3F",
    BORDER_ROW="#242B3B",
    BORDER_INPUT="#39425A",
    TEXT="#EDEAE2",
    MUTED="#8E97A9",
    MUTED2="#9AA3B4",
    SUBTLE="#B7BECC",
    ICON="#A6AEBC",
    SECONDARY="#B9C0CC",
    BODY="#D6D9E0",
    FAINT="#727C90",
    DISABLED="#5A6478",
    PRESSED_TEXT="#EDEAE2",
    FOCUS_HOVER="#3C465E",
    FOCUS_PRESS="#4A5470",
    PRESS_BG="#2A3144",
    SPLITTER_HOVER="#3A445C",
    TINT="#3A2622",
    TINT_HOVER="#4A2E28",
    SCROLL="#465068",
)
# 奶油胶片 —— 明显偏暖黄的羊皮纸/旧胶片质感，与晨光的中性象牙白区分。
# 地面色加黄、辅色加棕，但仍只三个色相，朱红点缀不变。
_CREAMY = _palette(
    BG="#F1E5CD",
    SURFACE="#FDF7E8",
    SURFACE_ALT="#F5E9D2",
    SURFACE_STRIPE="#F9EFDC",
    SURFACE_HOVER="#EEDDBE",
    BORDER="#E3D3B4",
    BORDER_SOFT="#EBDCC2",
    BORDER_ROW="#E8D9BC",
    BORDER_INPUT="#D6C39E",
    TEXT="#33281A",
    MUTED="#8F8067",
    MUTED2="#89795E",
    SUBTLE="#695C45",
    ICON="#655740",
    SECONDARY="#5D513D",
    BODY="#413526",
    FAINT="#AB9C81",
    DISABLED="#CBBDA0",
    PRESSED_TEXT="#33281A",
    FOCUS_HOVER="#D8C39C",
    FOCUS_PRESS="#C4AC7E",
    PRESS_BG="#EEDDBE",
    SPLITTER_HOVER="#DCCCA9",
    TINT="#F7DCCB",
    TINT_HOVER="#F3C8B0",
    SCROLL="#CDBD9C",
)

_MIST = _palette(
    BG="#F4F6F8",
    SURFACE="#FFFFFF",
    SURFACE_ALT="#EFF2F5",
    SURFACE_STRIPE="#FAFBFC",
    SURFACE_HOVER="#E6EBF1",
    BORDER="#DCE3EA",
    BORDER_SOFT="#E7ECF1",
    BORDER_ROW="#E4EAF0",
    BORDER_INPUT="#D2DCE6",
    TEXT="#2E3A4B",
    MUTED="#8A96A6",
    MUTED2="#8491A2",
    SUBTLE="#5F6E80",
    ICON="#5B6A7C",
    SECONDARY="#536275",
    BODY="#3B4A5C",
    FAINT="#A9B3BF",
    DISABLED="#C7D0D9",
    PRESSED_TEXT="#2E3A4B",
    FOCUS_HOVER="#B9C6D4",
    FOCUS_PRESS="#A5B5C6",
    PRESS_BG="#E6EBF1",
    SPLITTER_HOVER="#CFD9E3",
    TINT="#FDEDE8",
    TINT_HOVER="#FBE0D9",
    SCROLL="#C4CFD9",
)

PALETTES: dict[str, dict[str, str]] = {
    THEME_DAYLIGHT: _DAYLIGHT,
    THEME_MIDNIGHT: _MIDNIGHT,
    THEME_CREAMY: _CREAMY,
    THEME_MIST: _MIST,
}

# 主题切换时自定义绘制需要这组"语义色"，随主题变化。
SEMANTIC_TOKENS = (
    "ACCENT", "ACCENT_DEEP", "TEXT", "MUTED2", "FAINT", "ICON",
    "SURFACE", "SURFACE_ALT", "SURFACE_STRIPE", "TINT", "BORDER",
)


def semantic_colors(theme_id: str) -> dict[str, str]:
    palette = PALETTES.get(theme_id, PALETTES[DEFAULT_THEME])
    return {key: palette[key] for key in SEMANTIC_TOKENS}


def stylesheet(theme_id: str, template: str) -> str:
    """渲染指定主题的 QSS。template 为 theme.py 中的 token 化模板。"""
    palette = PALETTES.get(theme_id, PALETTES[DEFAULT_THEME])
    return _render(template, palette)
