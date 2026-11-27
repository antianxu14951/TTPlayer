"""The playing queue, folded out of the player bar.

QQ Music's pattern: the queue is not a destination in the sidebar, it is a
sheet that drops out of the transport's right corner, right above the button
that opened it. The sheet is a ``Qt.Popup`` on purpose — clicking anywhere
outside closes it, no overlay or modal state is needed, and the library stays
interactive underneath.

Everything it shows comes from :class:`ui.queue_view.QueueView`; this module
only owns the frame, the header and the anchored placement.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.models import Track
from ui.queue_view import QueueView

WIDTH = 440
HEIGHT = 476
# Room around the card so the drop shadow has somewhere to fall.
SHADOW_MARGIN = 14


class QueuePopup(QWidget):
    """Anchored sheet listing the playing queue."""

    playRequested = Signal(int)
    removeRequested = Signal(list)
    clearRequested = Signal()
    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setObjectName("queuePopup")
        self.setFixedSize(WIDTH, HEIGHT)
        self._build_ui()

    # ---------- build ----------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN
        )
        outer.setSpacing(0)

        card = QFrame()
        card.setObjectName("queuePopupCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(23, 31, 48, 46))
        card.setGraphicsEffect(shadow)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("正在播放")
        title.setObjectName("popupTitle")
        self.count_label = QLabel("")
        self.count_label.setObjectName("popupCount")
        self.clear_button = QPushButton("清空")
        self.clear_button.setObjectName("popupAction")
        self.clear_button.setToolTip("清空正在播放列表（不影响媒体库与歌单）")
        self.clear_button.clicked.connect(self.clearRequested.emit)
        header.addWidget(title)
        header.addWidget(self.count_label)
        header.addStretch(1)
        header.addWidget(self.clear_button)
        layout.addLayout(header)

        # Denser than the library table: the sheet shows the queue at a
        # glance, so more rows matter than breathing room.
        self.queue_view = QueueView(
            card, row_height=44, number_width=44, striped=False
        )
        self.queue_view.playRequested.connect(self.playRequested.emit)
        self.queue_view.removeRequested.connect(self.removeRequested.emit)
        self.queue_view.clearRequested.connect(self.clearRequested.emit)
        layout.addWidget(self.queue_view, stretch=1)

        outer.addWidget(card)

    # ---------- data ----------

    def set_queue(self, tracks: list[Track], current_id: str = "") -> None:
        self.queue_view.set_tracks(tracks, current_id)
        self.count_label.setText(f"{len(tracks)} 首")
        self.clear_button.setEnabled(bool(tracks))

    def row_count(self) -> int:
        return self.queue_view.row_count()

    def focus_current(self) -> None:
        self.queue_view.focus_current()

    # ---------- placement ----------

    def open_at(self, anchor: QWidget) -> None:
        """Open the sheet above ``anchor``, right edges aligned.

        The anchor is the transport's queue key in the bottom-right corner, so
        the sheet grows upwards from it and stays inside the screen even when
        the window sits against a screen edge.
        """
        rect = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
        screen = QApplication.screenAt(rect.center()) or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)
        self.move(self.anchoring_position(rect, area))
        self.focus_current()
        self.show()

    def anchoring_position(self, anchor: QRect, area: QRect) -> QPoint:
        """Top-left for a sheet above ``anchor``, kept inside ``area``.

        Kept separate from ``open_at`` so the placement rule can be tested
        without a window manager.
        """
        x = anchor.right() - self.width() + 1
        y = anchor.top() - self.height() - 6
        x = max(area.left() + 8, min(x, area.right() - self.width() - 8))
        y = max(area.top() + 8, y)
        return QPoint(x, y)

    def toggle_at(self, anchor: QWidget) -> None:
        if self.isVisible():
            self.hide()
            return
        self.open_at(anchor)

    # ---------- events ----------

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        self.closed.emit()
        super().hideEvent(event)

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        # Escape closes like every other sheet in the app.
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
