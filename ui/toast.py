"""Non-blocking green toast for reminder-level feedback.

A toast is deliberately *not* a QMessageBox: it has no buttons, never takes
focus, never plays the system notification sound, and fades away on its own.
Anything that needs a real decision (confirm / danger) stays a modal dialog in
``ui.texts`` -- this module only handles "tell the user, then get out of the
way" messages.
"""
from __future__ import annotations

from PySide6.QtCore import QPropertyAnimation, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

# A calm, confident green. Reminder feedback is a functional semantic colour,
# intentionally outside the brand triad -- like a success state in any modern
# app. It stays flat (no gradient) so it never competes with the accent red.
BG = "#1F9D6A"
BORDER = "#18845A"
TITLE_COLOR = "#FFFFFF"
BODY_COLOR = "#E6F7EF"
RADIUS = 12
ANIM_MS = 180
DEFAULT_DURATION_MS = 3000

_active_toasts: list["Toast"] = []


def _check_pixmap(size: int = 18) -> QPixmap:
    """Draw a white circled check with QPainter (no font glyph, no tofu)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#FFFFFF"))
    pen.setWidthF(1.6)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(1.5, 1.5, size - 3, size - 3)
    # check mark
    pen.setWidthF(2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawLine(size * 0.31, size * 0.53, size * 0.45, size * 0.67)
    painter.drawLine(size * 0.45, size * 0.67, size * 0.72, size * 0.36)
    painter.end()
    return pixmap


class Toast(QWidget):
    """A frameless, click-through-free green notice anchored near the top."""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        text: str,
        detail: str = "",
        duration_ms: int = DEFAULT_DURATION_MS,
    ) -> None:
        super().__init__(None)  # top-level so it can float above everything
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowModality(Qt.WindowModality.NonModal)

        root = QHBoxLayout(self)
        root.setContentsMargins(16, 12, 18, 12)
        root.setSpacing(12)

        icon = QLabel()
        icon.setPixmap(_check_pixmap())
        icon.setFixedSize(18, 18)
        root.addWidget(icon, alignment=Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"color: {TITLE_COLOR}; font-size: 13px; font-weight: 600; "
            "background: transparent; border: 0;"
        )
        column.addWidget(title_label)

        body = " ".join(piece for piece in (text, detail) if piece)
        if body:
            body_label = QLabel(body)
            body_label.setStyleSheet(
                f"color: {BODY_COLOR}; font-size: 12px; "
                "background: transparent; border: 0;"
            )
            body_label.setWordWrap(True)
            body_label.setMinimumWidth(300)
            body_label.setMaximumWidth(440)
            column.addWidget(body_label)
        root.addLayout(column)

        self.setMinimumWidth(340)
        self.setMaximumWidth(520)
        self.adjustSize()

        # Anchor to the top-centre of the parent window.
        if parent is not None:
            geo = parent.frameGeometry()
            self.move(
                geo.x() + (geo.width() - self.width()) // 2,
                geo.y() + 16,
            )
        else:
            self.move(0, 0)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(ANIM_MS)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

        while _active_toasts:
            _active_toasts[0].dismiss()
        _active_toasts.append(self)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self._timer.start(max(ANIM_MS, duration_ms))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(BORDER))
        painter.drawRoundedRect(QRectF(self.rect()), RADIUS + 1, RADIUS + 1)
        painter.setBrush(QColor(BG))
        painter.drawRoundedRect(
            QRectF(self.rect()).adjusted(1, 1, -1, -1), RADIUS, RADIUS
        )
        painter.end()

    def dismiss(self) -> None:
        """Fade out, then release the C++ object. Safe to call repeatedly."""
        if not self.isVisible() or getattr(self, "_closing", False):
            return
        self._closing = True
        self._timer.stop()
        if self in _active_toasts:
            _active_toasts.remove(self)
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._finish)
        self._fade.start()

    def _finish(self) -> None:
        try:
            self.close()
        finally:
            self.deleteLater()
