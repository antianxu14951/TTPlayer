"""Bright, youthful flat light theme with one indigo focal point.

Design tokens (QSS has no variables, so values are inlined consistently):
canvas #F7F4EE / surface #ffffff / surface-alt #FBF9F5 / border #EAE6DE
text #1E2333 / secondary #596073 / muted #939AA9
primary #F5513A (indigo) / primary-light #FF6B4A / success #1E2333 / danger #C23824
radius 10px (controls) / 14-16px (cards) / 6px (small chips)
Body and secondary text meet WCAG AA (4.5:1) on white. Depth comes from the
cool canvas under pure-white cards plus one hairline border — no drop shadows,
no heavy pills. A single indigo gradient (brand card, primary buttons, play
key, active slider) is the only decorative flourish, so the eye has one place
to land instead of a page of competing outlines.

Button system - three weights, one geometry:
  fill     #ffffff  border #E4E0D7  hover #FDEDE8  press #FDEDE8
  primary  indigo gradient (#FF6B4A -> #D8402C)   disabled #F4A99C
  ghost    none     hover #F5F1E9   press #e8eaf6
  md  13px / 0 18px / height 38 / radius 10   (lines up with an input)
  sm  12px / 0 12px / height 34 / radius 10   (player bar, topbar)
  xs  12px / 0 14px / height 26 / radius 8    (inside cards)
Heights are pinned with min/max-height: Qt drops a border-radius that gets
too close to half the natural height, so unpinned controls could silently turn
into sharp rectangles. Every state is declared so none falls back to Qt.
"""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

# Semantic tokens for the few colours painted by QPainter / QColor instead of
# QSS. Keep them in sync with the inlined values in LIGHT_STYLESHEET below so a
# re-theme never leaves a stale accent in the table or on an icon.
COLOR_PRIMARY = "#F5513A"
COLOR_ICON = "#5C6475"
COLOR_MUTED = "#939AA9"
COLOR_ON_PRIMARY = "#ffffff"
COLOR_CURRENT_ROW_BG = "#FDEDE8"
COLOR_CURRENT_ROW_TEXT = "#D8402C"

# Queue popup reuses the same tokens (imported, not copied) so a re-theme can
# never leave the folded-out list on an old highlight colour.



def transparent_panel(widget: QWidget) -> QWidget:
    """Layout-only container: return it unchanged.

    Qt plain widgets already paint no background of their own, so these
    containers never needed a transparency attribute. Kept as a no-op helper
    so call sites stay readable; no UI component gets an alpha value.
    """
    return widget


class _ButtonCursorFilter(QObject):
    """Give every button the same pointing hand.

    Qt's style-sheet language has no ``cursor`` property, and buttons are
    created all over the app (including dialogs opened long after startup),
    so the hand is applied on show/enable instead of at each call site.
    """

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() in (QEvent.Type.Show, QEvent.Type.EnabledChange) and isinstance(
            obj, QPushButton
        ):
            shape = (
                Qt.CursorShape.PointingHandCursor
                if obj.isEnabled()
                else Qt.CursorShape.ArrowCursor
            )
            if obj.cursor().shape() != shape:
                obj.setCursor(shape)
        return False


def install_button_cursors(app: QApplication) -> None:
    """Apply the shared cursor to buttons now and to any created later."""
    if app.property("buttonCursorFilter") is not None:
        return
    handler = _ButtonCursorFilter(app)
    app.installEventFilter(handler)
    # The filter is parented to the app, so it outlives this call.
    app.setProperty("buttonCursorFilter", handler)


# _STYLESHEET_TEMPLATE is the single source of truth: one QSS skeleton with
# @@TOKEN@@ placeholders. themes.py renders it into a concrete stylesheet.
_STYLESHEET_TEMPLATE = """
QWidget {
    /* Only windows and dedicated surfaces paint a background. Giving every
       QWidget a background makes labels and layout containers leave ugly
       rectangular patches on top of cards. */
    background: transparent;
    color: @@TEXT@@;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                 "Noto Sans SC", "DengXian", "SimHei", "Segoe UI", sans-serif;
    font-size: 13px;
}
QMainWindow { background: @@BG@@; }
QDialog { background: @@BG@@; }

/* ---------- Sidebar ---------- */
#sidebar {
    background: @@SURFACE@@;
    border-right: 1px solid @@BORDER@@;
}
/* The emblem already carries the entire identity (ink tile / cream disc /
   vermilion T). Nesting it in a second ink tile only repeated the silhouette
   and pushed white + pale-pink text into a cramped corner. The mark now sits
   directly on the sidebar: one clean lockup, ink wordmark, neutral tagline. */
#brandCard {
    background: transparent;
    border: 0;
}
#brandLogo { background: transparent; }
#brandName { color: @@TEXT@@; font-size: 19px; font-weight: 700; }
#brandTagline { color: @@MUTED@@; font-size: 11px; }
#statsCard {
    background: @@SURFACE_ALT@@;
    border: 1px solid @@BORDER_SOFT@@;
    border-radius: 12px;
    padding: 10px 8px;
    color: @@SECONDARY@@;
    font-size: 12px;
}
#sectionLabel {
    background: transparent;
    color: @@MUTED@@;
    font-size: 11px;
    font-weight: 700;
    padding: 16px 20px 8px 20px;
}
#navButton {
    background: transparent;
    border: 0;
    border-radius: 12px;
    min-height: 38px;
    max-height: 38px;
    text-align: left;
    padding: 0 16px;
    color: @@ICON@@;
    font-size: 13px;
}
#navButton:hover { background: @@SURFACE_HOVER@@; color: @@ACCENT_DEEP@@; }
#navButton:pressed { background: @@PRESS_BG@@; color: @@PRESSED_TEXT@@; }
#navButton:checked {
    background: @@TINT@@;
    color: @@ACCENT_DEEP@@;
    font-weight: 600;
}
#navButton:checked:hover { background: @@TINT_HOVER@@; }
#playlistList {
    background: transparent;
    border: 0;
    padding: 0 9px;
}
#playlistList::item {
    padding: 10px 12px;
    border-radius: 12px;
    color: @@ICON@@;
}
#playlistList::item:hover { background: @@SURFACE_ALT@@; }
#playlistList::item:selected { background: @@TINT@@; color: @@ACCENT_DEEP@@; font-weight: 600; }

/* ---------- Top bar & content ---------- */
#topbar {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER@@;
    border-radius: 14px;
}
#viewTitle { font-size: 18px; font-weight: 700; color: @@TEXT@@; }
#viewSubtitle { color: @@MUTED@@; font-size: 11px; }
#trackTable {
    background: @@SURFACE@@;
    alternate-background-color: @@SURFACE_STRIPE@@;
    gridline-color: @@BORDER_SOFT@@;
    border: 1px solid @@BORDER@@;
    border-radius: 14px;
    selection-background-color: @@TINT@@;
    selection-color: @@TEXT@@;
}
QHeaderView::section {
    background: @@SURFACE_ALT@@;
    color: @@SUBTLE@@;
    font-size: 12px;
    font-weight: 700;
    border: 0;
    border-bottom: 1px solid @@BORDER_SOFT@@;
    text-align: left;
    padding: 11px 14px;
}
#trackTable::item { padding: 9px 10px; border-bottom: 1px solid @@BORDER_ROW@@; }

/* ---------- Player bar ---------- */
#playerBar {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER@@;
    border-radius: 14px;
}
/* One quiet line under the player: the copyright stand is not a banner. */
#copyrightNotice {
    color: @@FAINT@@;
    font-size: 11px;
    padding: 1px 4px 0 4px;
}
#coverLabel {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 @@TINT@@, stop:1 @@TINT_HOVER@@);
    border-radius: 10px;
    color: @@ACCENT@@;
    font-size: 22px;
}
#trackTitle { font-size: 14px; font-weight: 600; }
#trackSubtitle { color: @@MUTED2@@; font-size: 12px; }
#timeLabel { color: @@MUTED2@@; font-size: 12px; }
#volumeIcon { background: transparent; }
/* Player transport: ghost icon keys, one primary, one icon rail.
   The utility keys share the control keys' language but are smaller and
   square, so the right edge reads as one row instead of a stack. */
#utilityButton {
    background: transparent;
    border: 0;
    border-radius: 10px;
    min-width: 34px;
    max-width: 34px;
    min-height: 34px;
    max-height: 34px;
    padding: 0;
}
#utilityButton:hover { background: @@SURFACE_HOVER@@; }
#utilityButton:pressed { background: @@BORDER@@; }
/* Volume is a supporting control: a thinner rail in a quieter tone than the
   seek bar, so the two never compete. */
#volumeSlider::groove:horizontal {
    height: 4px;
    background: @@BORDER_INPUT@@;
    border-radius: 2px;
}
#volumeSlider::sub-page:horizontal { background: @@ICON@@; border-radius: 2px; }
/* Qt paints a slider handle as (width + 2*border) x (groove + 2*|margin|) and
   falls back to a sharp rectangle as soon as the radius exceeds half the
   height, so the three numbers have to agree for a round knob: 8 + 4 = 4 + 8
   = 12, radius 6. */
#volumeSlider::handle:horizontal {
    width: 8px;
    height: 8px;
    margin: -4px 0;
    background: @@SURFACE@@;
    border: 2px solid @@ICON@@;
    border-radius: 6px;
}
#volumeSlider::handle:horizontal:hover { border-color: @@TEXT@@; }
#controlButton {
    background: transparent;
    border: 0;
    border-radius: 18px;
    min-width: 38px;
    max-width: 38px;
    min-height: 38px;
    max-height: 38px;
    padding: 0;
    font-size: 13px;
    color: @@ICON@@;
}
#controlButton:hover { background: @@SURFACE_HOVER@@; color: @@ACCENT_DEEP@@; }
#controlButton:pressed { background: @@PRESS_BG@@; color: @@PRESSED_TEXT@@; }
#playButton {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 @@ACCENT_HOVER@@, stop:1 @@ACCENT_DEEP@@);
    color: @@ON_ACCENT@@;
    border: 0;
    border-radius: 21px;
    min-width: 46px;
    max-width: 46px;
    min-height: 46px;
    max-height: 46px;
    padding: 0;
    font-size: 15px;
}
#playButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 @@ACCENT_HOVER_LIGHT@@, stop:1 @@ACCENT_HOVER_DEEP@@);
}
#playButton:pressed { background: @@ACCENT_PRESS@@; }

/* Queue key + the sheet it folds out. The card draws the frame, so the list
   inside drops its own border and background. */
#queueBadge {
    background: @@ACCENT@@;
    color: @@ON_ACCENT@@;
    border-radius: 8px;
    font-size: 10px;
    font-weight: 700;
}
#queuePopup { background: transparent; }
#queuePopupCard {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER@@;
    border-radius: 16px;
}
#queuePopupCard QTableWidget#trackTable {
    background: transparent;
    border: 0;
    border-radius: 0;
}
/* The empty state lives inside the card, so it must not draw a second
   frame of its own. */
#queuePopupCard #emptyPage {
    background: transparent;
    border: 0;
    border-radius: 0;
}
#queuePopupCard QHeaderView::section {
    background: transparent;
    border-bottom: 1px solid @@BORDER_SOFT@@;
}
#popupTitle { font-size: 13px; font-weight: 600; color: @@TEXT@@; }
#popupCount { color: @@MUTED2@@; font-size: 11px; }
#popupAction {
    background: transparent;
    border: 0;
    border-radius: 12px;
    min-height: 24px;
    max-height: 24px;
    padding: 0 10px;
    color: @@SECONDARY@@;
    font-size: 12px;
}
#popupAction:hover { background: @@SURFACE_HOVER@@; color: @@ACCENT_DEEP@@; }
#popupAction:disabled { background: transparent; color: @@DISABLED@@; }

/* ---------- Buttons & inputs ---------- */
/* Fill (secondary) is the default of every QPushButton in the app.
   Heights are pinned: Qt silently drops a border-radius that sits too close
   to half the *natural* height, so unpinned buttons used to flip between a
   pill and a sharp rectangle depending on font metrics. */
QPushButton {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER_INPUT@@;
    border-radius: 19px;
    min-height: 38px;
    max-height: 38px;
    padding: 0 18px;
    color: @@BODY@@;
    font-size: 13px;
}
QPushButton:hover { background: @@TINT@@; border-color: @@FOCUS_HOVER@@; color: @@PRESSED_TEXT@@; }
QPushButton:pressed { background: @@TINT_HOVER@@; border-color: @@FOCUS_PRESS@@; }
QPushButton:disabled { color: @@DISABLED@@; background: @@BG@@; border-color: @@BORDER_SOFT@@; }
/* Keyboard focus reads as a tinted edge instead of Qt's dotted rectangle. */
QPushButton:focus { border-color: @@TEXT@@; outline: none; }
#primaryButton {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 @@ACCENT_HOVER@@, stop:1 @@ACCENT_DEEP@@);
    color: @@ON_ACCENT@@;
    border: 0;
    border-radius: 10px;
    min-height: 38px;
    max-height: 38px;
    padding: 0 20px;
    font-size: 13px;
    font-weight: 600;
}
#primaryButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 @@ACCENT_HOVER_LIGHT@@, stop:1 @@ACCENT_HOVER_DEEP@@);
}
#primaryButton:pressed { background: @@ACCENT_PRESS@@; border: 0; }
#primaryButton:disabled { background: @@ACCENT_DISABLED@@; border: 0; color: @@ON_ACCENT@@; }
/* Inputs share the 40px height and 12px radius with the md button. */
QLineEdit, QPlainTextEdit {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER_INPUT@@;
    border-radius: 12px;
    padding: 10px 12px;
    color: @@TEXT@@;
    selection-background-color: @@TINT_HOVER@@;
}
QLineEdit:focus, QPlainTextEdit:focus { border-color: @@TEXT@@; }

/* ---------- Sliders & scrollbars ---------- */
QSlider::groove:horizontal {
    height: 5px;
    background: @@BORDER_INPUT@@;
    border-radius: 3px;
}
QSlider::sub-page:horizontal { background: @@ACCENT@@; border-radius: 3px; }
/* Same geometry rule as the volume knob: 9 + 2*2 = 5 + 2*4 = 13, radius 6. */
QSlider::handle:horizontal {
    width: 9px;
    height: 9px;
    margin: -4px 0;
    background: @@SURFACE@@;
    border: 2px solid @@ACCENT@@;
    border-radius: 6px;
}
QSlider::handle:horizontal:hover { border-color: @@ACCENT_DEEP@@; background: @@BORDER_SOFT@@; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: @@SCROLL@@; border-radius: 5px; min-height: 26px; }
QScrollBar::handle:vertical:hover { background: @@DISABLED@@; }
/* No arrow keys: a 10px bar turns them into stray dots on a light card. */
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QTableCornerButton::section { background: transparent; border: 0; }

/* ---------- Menus & dialogs ---------- */
QMenu {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER@@;
    border-radius: 12px;
    padding: 6px;
}
QMenu::item { padding: 7px 22px; border-radius: 8px; color: @@BODY@@; }
QMenu::item:selected { background: @@TINT_HOVER@@; color: @@ACCENT_DEEP@@; }
QMessageBox, QDialog { background: @@BG@@; }
#dialogTitle { font-size: 19px; font-weight: 700; color: @@TEXT@@; }
#dialogHint { color: @@MUTED2@@; font-size: 12px; }
#fieldLabel { color: @@ICON@@; font-size: 12px; font-weight: 600; }

/* ---------- Import progress ---------- */
#progressCard {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER@@;
    border-radius: 16px;
}
#stageLabel { font-size: 14px; font-weight: 600; color: @@TEXT@@; }
#percentLabel { font-size: 18px; font-weight: 700; color: @@ACCENT@@; }
#importProgressBar {
    background: @@BORDER@@;
    border: 0;
    border-radius: 6px;
    height: 12px;
    text-align: center;
}
#importProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 @@ACCENT_HOVER@@, stop:1 @@ACCENT@@);
    border-radius: 6px;
}
#importProgressBar[state="error"]::chunk { background: @@ACCENT_PRESS@@; }
#stepDetail { color: @@SECONDARY@@; font-size: 12px; }
#stepList {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER@@;
    border-radius: 14px;
    padding: 6px;
}
#stepList::item {
    padding: 8px 12px;
    border-radius: 10px;
    color: @@SECONDARY@@;
    font-size: 12px;
}
#statusLabel { color: @@ICON@@; font-size: 12px; }

/* ---------- Settings ---------- */
#settingsCard {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER@@;
    border-radius: 16px;
}
#settingsSection { font-size: 14px; font-weight: 700; color: @@TEXT@@; }
#settingsValue { color: @@SECONDARY@@; font-size: 12px; }
#settingsHint { color: @@MUTED2@@; font-size: 11px; }
/* Theme picker: four equal chips; the checked one gets the accent edge. */
#themeButton {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER_INPUT@@;
    border-radius: 10px;
    min-height: 34px;
    max-height: 34px;
    padding: 0 12px;
    color: @@BODY@@;
    font-size: 12px;
}
#themeButton:hover { background: @@SURFACE_HOVER@@; border-color: @@ACCENT@@; }
#themeButton:checked {
    background: @@TINT@@;
    border: 1px solid @@ACCENT@@;
    color: @@ACCENT_DEEP@@;
    font-weight: 600;
}
QScrollArea { background: transparent; border: 0; }
/* Same 40px box as QLineEdit so a row of input + button lines up. */
QComboBox, QSpinBox {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER_INPUT@@;
    border-radius: 12px;
    padding: 10px 12px;
    color: @@TEXT@@;
}
QComboBox:hover, QSpinBox:hover { border-color: @@FOCUS_HOVER@@; }
QComboBox:focus, QSpinBox:focus { border-color: @@TEXT@@; }
QComboBox QAbstractItemView {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER@@;
    border-radius: 10px;
    selection-background-color: @@TINT@@;
    selection-color: @@TEXT@@;
    padding: 4px;
}

/* ---------- Aggregated search ---------- */
/* One card holds the field, the rows and the status: the panel is the lower
   half of the main screen, not a page with a hero of its own. */
#searchCard {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER@@;
    border-radius: 16px;
}
#searchEmpty { background: transparent; }
#emptyTitleSmall { font-size: 13px; font-weight: 600; color: @@TEXT@@; }
/* The playlist table and the search panel share one screen; the divider is
   invisible until you reach for it. */
QSplitter#contentSplitter::handle:vertical {
    height: 10px;
    background: transparent;
}
QSplitter#contentSplitter::handle:vertical:hover {
    background: @@SPLITTER_HOVER@@;
    border-radius: 3px;
}
/* The search field lives in the topbar; it is pinned to the same 34px
   box as the topbar buttons so the whole row reads as one line. */
#searchInput {
    background: @@SURFACE_ALT@@;
    border: 1px solid @@BORDER_INPUT@@;
    border-radius: 10px;
    min-height: 34px;
    max-height: 34px;
    padding: 0 12px;
    font-size: 13px;
    color: @@TEXT@@;
}
#searchInput:focus {
    border-color: @@TEXT@@;
    background: @@SURFACE@@;
}
/* One geometry spec for every topbar button: 34px pill, 13px font — the
   height of the search field exactly. The variant property only picks the
   colour, never the shape. */
#topbarButton {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER_INPUT@@;
    border-radius: 10px;
    min-height: 34px;
    max-height: 34px;
    padding: 0 18px;
    color: @@BODY@@;
    font-size: 13px;
}
#topbarButton:hover { background: @@TINT@@; border-color: @@FOCUS_HOVER@@; color: @@PRESSED_TEXT@@; }
#topbarButton:pressed { background: @@TINT@@; border-color: @@FOCUS_PRESS@@; }
#topbarButton:focus { border-color: @@TEXT@@; outline: none; }
#topbarButton[variant="primary"] {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 @@ACCENT_HOVER@@, stop:1 @@ACCENT_DEEP@@);
    color: @@ON_ACCENT@@;
    border: 0;
    font-weight: 600;
}
#topbarButton[variant="primary"]:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 @@ACCENT_HOVER_LIGHT@@, stop:1 @@ACCENT_HOVER_DEEP@@);
}
#topbarButton[variant="primary"]:pressed { background: @@ACCENT_PRESS@@; }
#topbarButton[variant="primary"]:disabled { background: @@ACCENT_DISABLED@@; color: @@ON_ACCENT@@; }
#qualityButton {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER_INPUT@@;
    border-radius: 14px;
    min-height: 28px;
    max-height: 28px;
    padding: 0 30px 0 14px;
    font-size: 12px;
    color: @@BODY@@;
    text-align: left;
}
#qualityButton:hover { background: @@TINT@@; border-color: @@FOCUS_HOVER@@; color: @@PRESSED_TEXT@@; }
#qualityButton:pressed { background: @@TINT_HOVER@@; border-color: @@FOCUS_PRESS@@; }
#listHeading { font-size: 14px; font-weight: 700; color: @@TEXT@@; }
#listCount { color: @@MUTED2@@; font-size: 12px; }
#searchMeta { color: @@MUTED2@@; font-size: 12px; }
#searchStatus { color: @@MUTED2@@; font-size: 11px; }
#songListCard {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER@@;
    border-radius: 16px;
}
#songList { background: transparent; border: 0; }
#songList::item { border: 0; background: transparent; }
#emptyPage {
    background: @@SURFACE@@;
    border: 1px solid @@BORDER@@;
    border-radius: 16px;
}
#emptyBadge {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 @@TINT@@, stop:1 @@TINT_HOVER@@);
    color: @@ACCENT@@;
    border-radius: 32px;
    font-size: 24px;
}
#emptyTitle { font-size: 15px; font-weight: 700; color: @@TEXT@@; }
#emptyHint { color: @@MUTED2@@; font-size: 12px; }
"""


# ---------------------------------------------------------------------------
# Theme application
# ---------------------------------------------------------------------------
from ui.themes import (  # noqa: E402 - imported here to avoid a circular import
    DEFAULT_THEME,
    PALETTES,
    THEMES,
    ThemeInfo,
    semantic_colors,
    stylesheet as _render_stylesheet,
)

# 当前生效的语义色（QPainter / QColor 绘制路径动态读取这里）。
# 默认填入暖白晨光，与上面的原始常量保持一致。
_current_theme_id: str = DEFAULT_THEME
_semantic: dict[str, str] = semantic_colors(DEFAULT_THEME)


def current_theme_id() -> str:
    return _current_theme_id


def current_semantic_colors() -> dict[str, str]:
    """返回当前主题的语义色副本，供自定义绘制模块取色。"""
    return dict(_semantic)


def theme_color(token: str) -> str:
    """取当前主题的单个语义色；未知 token 回退到默认主题。"""
    return _semantic.get(token, PALETTES[DEFAULT_THEME].get(token, "#000000"))


def stylesheet_for(theme_id: str) -> str:
    """渲染指定主题的 QSS（幂等，可安全重复调用）。"""
    return _render_stylesheet(theme_id, _STYLESHEET_TEMPLATE)


# 背景层置顶绘制，所有组件保持各自主题的不透明实色外观。
# 透明度只作用于背景图层本身，绝不给任何组件加 alpha——这是用户明确要求：
# "只给背景图片/视频加透明度"，组件像素不参与任何混合，低透明度时原样清晰。
_background_active: bool = False


def is_background_active() -> bool:
    """是否设置了背景（供外部查询；不影响组件外观）。"""
    return _background_active


def set_background_active(active: bool, widget: QWidget) -> None:
    """记录背景状态。组件样式不因此改变——透明度只在背景图层自身生效。"""
    global _background_active
    _background_active = bool(active)


def _compose_stylesheet() -> str:
    """当前主题的完整 QSS，纯主题外观，不追加任何面板透明覆盖层。"""
    return stylesheet_for(_current_theme_id)


def available_themes() -> list[ThemeInfo]:
    return list(THEMES.values())


def apply_theme(theme_id: str, widget: QWidget) -> None:
    """立即切换主题：换 QSS + 同步语义色，widget 通常是 MainWindow。

    语义色是模块级可变状态，QSS 之外的 QPainter 绘制路径通过 theme_color()
    读取它，因此切换后重绘的控件会用新色。图标是烘焙好的 QPixmap，需要由
    调用方（MainWindow）显式重设。
    """
    global _current_theme_id, _semantic
    if theme_id not in PALETTES:
        theme_id = DEFAULT_THEME
    _current_theme_id = theme_id
    _semantic = semantic_colors(theme_id)
    widget.setStyleSheet(_compose_stylesheet())
