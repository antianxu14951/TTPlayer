"""The queue that will actually be heard, in order.

The library table answers "what do I own"; this view answers "what is queued
and what comes next". It deliberately borrows the library table's vocabulary —
same row height, same header, same cover thumbnails and "▸ 标题" marker for the
current track — so switching between the two views never feels like leaving the
app. The one addition is a number column: order is the whole point of a queue.

The view is presentation only. It reports intents (play this row, drop these
ids, clear everything) and the window decides what they mean, which keeps the
player free of any widget import.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config.settings import COVER_DIR
from core.models import Track
from ui.formatting import format_duration
from ui.icons import note_pixmap
from ui.marquee import MarqueeLabel
from ui.theme import theme_color

COLUMNS = ("#", "标题", "歌手", "专辑", "时长")

def _weighted(font, bold: bool):  # type: ignore[no-untyped-def]
    font.setBold(bold)
    return font


class QueueView(QWidget):
    """Table of the playing queue, plus an empty state when nothing is loaded."""

    playRequested = Signal(int)
    removeRequested = Signal(list)
    clearRequested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        row_height: int = 52,
        number_width: int = 52,
        striped: bool = True,
    ) -> None:
        super().__init__(parent)
        self._tracks: list[Track] = []
        self._ids: list[str] = []
        self._current_id = ""
        self._row_height = row_height
        self._number_width = number_width
        self._striped = striped
        self._build_ui()

    # ---------- build ----------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.stack = QStackedWidget()
        self.stack.setObjectName("queueStack")
        self.stack.addWidget(self._build_empty_page())
        self.stack.addWidget(self._build_table())
        layout.addWidget(self.stack)
        self.stack.setCurrentIndex(0)

    def _build_empty_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("emptyPage")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 24, 20, 24)
        layout.setSpacing(10)
        layout.addStretch(1)
        badge = QLabel()
        badge.setObjectName("emptyBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(64, 64)
        badge.setPixmap(note_pixmap(28, theme_color("ACCENT")))
        layout.addWidget(badge, alignment=Qt.AlignmentFlag.AlignHCenter)
        title = QLabel("正在播放列表是空的")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(title)
        hint = QLabel(
            "双击歌曲开始播放，整支队列会列在这里；"
            "可跳到任意一首，也可移除不想听的。"
        )
        hint.setObjectName("emptyHint")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        hint.setFixedWidth(320)
        hint.setMinimumHeight(36)
        layout.addWidget(hint, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        return page

    def _tail_widths(self) -> list[tuple[int, int]]:
        """歌手 / 专辑 / 时长的固定宽度。

        弹层内容宽只有 412px（WIDTH 440 减两侧 14px），再扣掉 # 列的
        44px 和表格自身的纵向滚动条，标题实际能拿到多少要看实测。

        取舍：歌手是「谁唱的」，一眼要认得出，给 4 字；专辑是补充
        信息，扫一眼知道是哪张就够，给 4 字略窄；时长是固定 mm:ss。
        这三列都不许超宽截断成「周杰…」那种认不出人的样子，但也不
        值得为长专辑名牺牲标题——所以宁可让它们各自用省略号收尾，
        把余量基本全部让给标题。标题越宽、真正需要跑马灯的歌越少，
        这正是目的：跑马灯只该发生在确实放不下的地方。
        """
        return [(2, 72), (3, 84), (4, 44)]

    def _build_table(self) -> QTableWidget:
        table = QTableWidget()
        # Same object name on purpose: the QSS for the library table already
        # defines the frame, header, striping and row separators.
        table.setObjectName("trackTable")
        table.setColumnCount(len(COLUMNS))
        table.setHorizontalHeaderLabels(list(COLUMNS))
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(self._striped)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.setShowGrid(False)
        table.verticalHeader().setDefaultSectionSize(self._row_height)
        header = table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(0, self._number_width)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(2, len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        # 标题是这列的主语：歌手/专辑只是补充，给固定宽度，
        # 剩下的全部让给标题。否则长歌手名会把标题挤成省略号。
        for column, width in self._tail_widths():
            table.setColumnWidth(column, width)
        table.doubleClicked.connect(self._on_double_click)
        table.customContextMenuRequested.connect(self._show_menu)
        self.table = table
        return table

    # ---------- data ----------

    def set_tracks(self, tracks: list[Track], current_id: str = "") -> None:
        """Rebuild the rows, or repaint only the current row when possible.

        ``queueChanged`` also fires on every skip, so the common case while
        listening is "same list, different current row". Comparing ids turns
        that into two colour changes instead of rebuilding every cell.
        """
        current_id = current_id or ""
        ids = [track.id for track in tracks]
        if ids and ids == self._ids:
            if current_id != self._current_id:
                previous, self._current_id = self._current_id, current_id
                for row, track in enumerate(self._tracks):
                    if track.id == previous or track.id == current_id:
                        self._style_row(row, track, track.id == current_id)
            return

        self._tracks = list(tracks)
        self._current_id = current_id
        self._ids = ids
        if not self._tracks:
            self.table.setRowCount(0)
            self.stack.setCurrentIndex(0)
            return

        scroll = self.table.verticalScrollBar().value()
        self.stack.setCurrentIndex(1)
        self.table.setRowCount(len(self._tracks))
        for row, track in enumerate(self._tracks):
            number = QTableWidgetItem(f"{row + 1:02d}")
            number.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 0, number)

            title_item = QTableWidgetItem("")
            title_item.setData(Qt.ItemDataRole.UserRole, track.id)
            self.table.setItem(row, 1, title_item)
            self.table.setCellWidget(
                row,
                1,
                self._make_title_cell(track, bool(current_id) and track.id == current_id),
            )

            self.table.setItem(row, 2, QTableWidgetItem(track.artist))
            self.table.setItem(row, 3, QTableWidgetItem(track.album))
            self.table.setItem(row, 4, QTableWidgetItem(format_duration(track.duration_ms)))

            self._style_row(row, track, bool(current_id) and track.id == current_id)

        self.table.verticalScrollBar().setValue(scroll)

    def _make_title_cell(self, track: Track, current: bool) -> QWidget:
        """标题列：封面缩略图 + 跑马灯文字。

        标题是这里唯一会超长的字段（歌手/专辑都是短词），所以只给
        标题用 MarqueeLabel。当前行加 "▸ " 前缀，但前缀不参与滚动
        判定——它占的宽度事先扣掉，否则一个刚放得下的名字会因为多了
        两个字符就开始晃。
        """
        cell = QWidget()
        cell.setObjectName("queueTitleCell")
        # 让表格自己的行底色（含当前行 TINT）透上来：
        # cell widget 会盖住 item 的背景，不透明的话当前行高亮会在
        # 标题这一列断掉，看起来像选中了半行。
        cell.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        cell.setAutoFillBackground(False)
        lay = QHBoxLayout(cell)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(6)

        prefix = "▸  " if current else "   "
        if track.cover_file:
            pixmap = QPixmap(str(COVER_DIR / track.cover_file))
            if not pixmap.isNull():
                thumb = QLabel()
                thumb.setFixedSize(28, 28)
                thumb.setPixmap(
                    pixmap.scaled(
                        28,
                        28,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                lay.addWidget(thumb)

        label = MarqueeLabel(prefix + track.title)
        label.setObjectName("queueTitleText")
        label.setProperty("currentTrack", current)
        label.setToolTip(track.title)
        lay.addWidget(label, stretch=1)
        cell.setProperty("trackId", track.id)
        return cell

    def _style_row(self, row: int, track: Track, current: bool) -> None:
        """Paint one row as current or ordinary, without touching its data."""
        number = self.table.item(row, 0)
        if number is not None:
            number.setForeground(QColor(theme_color("ACCENT_DEEP") if current else theme_color("FAINT")))
            number.setFont(_weighted(number.font(), current))

        title = self.table.cellWidget(row, 1)
        label = title.findChild(MarqueeLabel, "queueTitleText") if title else None
        if label is not None:
            prefix = "▸  " if current else "   "
            label.setText(prefix + track.title)
            label.set_color(theme_color("ACCENT_DEEP") if current else theme_color("TEXT"))
            label.setFont(_weighted(label.font(), current))
            label.refresh()

        background = QBrush(QColor(theme_color("TINT"))) if current else QBrush()
        for column in range(self.table.columnCount()):
            item = self.table.item(row, column)
            if item is not None:
                item.setBackground(background)

    def focus_current(self) -> None:
        """Scroll the playing track into view without selecting it.

        Selection is the user's; highlighting a row would make it look like
        something they clicked. The queue only needs to show where "now" is.
        """
        for row, track in enumerate(self._tracks):
            if self._current_id and track.id == self._current_id:
                self.table.scrollToItem(
                    self.table.item(row, 0),
                    QAbstractItemView.ScrollHint.PositionAtCenter,
                )
                return

    def row_count(self) -> int:
        return len(self._tracks)

    # ---------- interaction ----------

    def _on_double_click(self, index: Any) -> None:
        row = index.row()
        if 0 <= row < len(self._tracks):
            self.playRequested.emit(row)

    def _selected_ids(self) -> list[str]:
        result: list[str] = []
        for model_index in self.table.selectionModel().selectedRows():
            item = self.table.item(model_index.row(), 1)
            if item is not None:
                result.append(item.data(Qt.ItemDataRole.UserRole))
        return result

    def _show_menu(self, position: Any) -> None:
        index = self.table.indexAt(position)
        if not index.isValid():
            return
        self.table.setCurrentCell(index.row(), 0)
        track_ids = self._selected_ids()
        if not track_ids:
            return
        row = index.row()
        menu = QMenu(self)
        menu.addAction(
            "播放这首",
            lambda: self.playRequested.emit(row) if 0 <= row < len(self._tracks) else None,
        )
        menu.addAction(
            "从播放列表移除",
            lambda ids=list(track_ids): self.removeRequested.emit(ids),
        )
        menu.addSeparator()
        menu.addAction("清空播放列表", self.clearRequested.emit)
        menu.exec(self.table.mapToGlobal(position))
