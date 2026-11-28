"""Auto-scrolling label for text wider than its box."""
from __future__ import annotations

from typing import Any
import weakref

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

# Weak: the queue rebuilds its rows on every change, so a strong set would
# keep every label ever created alive and the tick would grow without bound.
_ACTIVE: weakref.WeakSet = weakref.WeakSet()
_DRIVER = None


class _Driver(QObject):
    """One timer that advances every registering label.

    A table with 300 songs would otherwise own 300 QTimers, every one of
    them firing at 12.5 Hz while only eight rows are on screen. One shared
    timer costs the same as a single label's timer would, and the tick is
    where the offscreen labels get filtered out.
    """

    def __init__(self, interval: int) -> None:
        super().__init__()
        self.interval = interval
        self._timer = QTimer(self)
        self._timer.setInterval(interval)
        self._timer.timeout.connect(self._tick)

    def add(self, label: MarqueeLabel) -> None:
        _ACTIVE.add(label)
        self._sync()

    def discard(self, label: MarqueeLabel) -> None:
        _ACTIVE.discard(label)
        self._sync()

    def _sync(self) -> None:
        if _ACTIVE and not self._timer.isActive():
            self._timer.start()
        elif not _ACTIVE and self._timer.isActive():
            self._timer.stop()

    def _tick(self) -> None:
        for label in list(_ACTIVE):
            # Do not evict onscreen-ness here: the label may scroll back into
            # the viewport later and no show() event will fire to re-register
            # it. Skip instead, and let the tick stay cheap because only the
            # visible rows ever repaint.
            if not label.is_shown_or_on_screen():
                continue
            label._advance()


def _driver_for(interval: int) -> _Driver:
    global _DRIVER
    if _DRIVER is None or _DRIVER.interval != interval:
        if _DRIVER is not None:
            _DRIVER._timer.stop()
        _DRIVER = _Driver(interval)
    return _DRIVER


class MarqueeLabel(QLabel):
    STEP = 1
    INTERVAL = 80
    GAP = 48

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        step: int = STEP,
        interval: int = INTERVAL,
        gap: int = GAP,
    ) -> None:
        super().__init__(text, parent)
        self._step = max(1, int(step))
        self._interval = max(16, int(interval))
        self._gap = max(0, int(gap))
        self._offset = 0
        self._color = None
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._sync()

    def setText(self, text: str) -> None:  # noqa: N802
        if text == self.text():
            return
        self._offset = 0
        super().setText(text)
        self._sync()

    def text_width(self) -> int:
        return QFontMetrics(self.font()).horizontalAdvance(self.text())

    def available_width(self) -> int:
        return self.contentsRect().width()

    def is_overflowing(self) -> bool:
        return self.text_width() > self.available_width()

    def is_scrolling(self) -> bool:
        return self in _ACTIVE

    def is_shown_or_on_screen(self) -> bool:
        """True only when this label actually sits inside a visible viewport.

        ``isVisible()`` answers "was show() called and hide() not called".
        Inside a QTableWidget that stays True for all 300 rows, including the
        290 scrolled far above and below the viewport. Ticking those is pure
        waste: the text is clipped away, but the timer still fires and the
        repaint still walks the widget tree.

        The test therefore asks the ancestors instead: walk up the parents
        and if any of them is a QAbstractScrollArea, compare this label's
        rectangle with that area's viewport. Outside means stop ticking.
        """
        if not self.isVisible() or not self.is_overflowing():
            return False

        from PySide6.QtWidgets import QAbstractScrollArea

        rect = self.rect()
        node = self.parentWidget()
        while node is not None:
            if isinstance(node, QAbstractScrollArea) and node.viewport().isVisible():
                viewport = node.viewport()
                local = QRect(self.mapTo(viewport, rect.topLeft()), rect.size())
                if not viewport.rect().intersects(local):
                    return False
            node = node.parentWidget()
        return True

    def refresh(self) -> None:
        self._sync()

    def set_color(self, color: str) -> None:
        self._color = QColor(color) if color else None
        self.update()

    def _sync(self) -> None:
        if not self.is_shown_or_on_screen():
            self._offset = 0
            self._stop()
            self.update()
            return
        self._start()

    def _start(self) -> None:
        if self in _ACTIVE:
            return
        _driver_for(self._interval).add(self)

    def _stop(self) -> None:
        if self not in _ACTIVE:
            return
        _ACTIVE.discard(self)
        if _DRIVER is not None:
            _DRIVER._sync()

    def start(self) -> None:
        self._sync()

    def stop(self) -> None:
        self._offset = 0
        self._stop()
        self.update()

    def _cycle_width(self) -> int:
        return self.text_width() + self._gap

    def _advance(self) -> None:
        span = self._cycle_width()
        if span <= 0:
            self._stop()
            return
        self._offset = (self._offset + self._step) % span
        self.update()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync()

    def showEvent(self, event: Any) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync()

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        super().hideEvent(event)
        # Stay registered: the label comes back on the next skip or scroll
        # and no show() may fire to re-add it. The tick simply skips hidden
        # labels, so this costs nothing.
        self._offset = 0
        self.update()

    def changeEvent(self, event: Any) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.Style,
        ):
            self._sync()

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        if self.wordWrap() or not self.is_overflowing():
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(self.font())

        rect = self.contentsRect()
        metrics = self.fontMetrics()
        if self._color is not None:
            painter.setPen(QColor(self._color))
        else:
            painter.setPen(QColor(self.palette().color(QPalette.ColorRole.WindowText)))

        text = self.text()
        span = self._cycle_width()
        y = rect.top() + (rect.height() - metrics.height()) // 2 + metrics.ascent()
        x = rect.left() - self._offset

        for _ in range(2):
            if x >= rect.right():
                break
            painter.drawText(QPoint(x, y), text)
            x += span

        painter.end()
