"""Vector glyphs for buttons.

Transport keys used to be text characters (``▶ ◀◀ ❚❚``). Those shapes come
from whichever font happens to be installed, so their weight, width and even
optical centre drift between machines. Everything here is drawn on one 24-unit
grid with QPainter and exported at 2x, which keeps the icon set consistent at
any DPI.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from ui.theme import theme_color

GRID = 24.0
SCALE = 2


def _canvas(size: int) -> tuple[QPixmap, QPainter]:
    pixmap = QPixmap(int(size * SCALE), int(size * SCALE))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    unit = (size * SCALE) / GRID
    painter.scale(unit, unit)
    painter.setPen(Qt.PenStyle.NoPen)
    return pixmap, painter


def _finish(pixmap: QPixmap, painter: QPainter) -> QPixmap:
    painter.end()
    pixmap.setDevicePixelRatio(SCALE)
    return pixmap


def _triangle(painter: QPainter, points: tuple[tuple[float, float], ...]) -> None:
    """A filled triangle with softened corners, like a printed play key."""
    path = QPainterPath()
    path.moveTo(*points[0])
    path.lineTo(*points[1])
    path.lineTo(*points[2])
    path.closeSubpath()
    painter.drawPath(path)


def transport_pixmap(kind: str, size: int = 16, color: str | None = None) -> QPixmap:
    """``play`` / ``pause`` / ``prev`` / ``next`` on a shared 24px grid."""
    color = color or theme_color("ICON")
    pixmap, painter = _canvas(size)
    painter.setBrush(QColor(color))

    if kind == "play":
        # Shifted half a unit right: a triangle's visual mass sits left of its
        # bounding box, so an unshifted one looks off-centre inside a circle.
        _triangle(painter, ((7.6, 3.6), (20.6, 12.0), (7.6, 20.4)))
    elif kind == "pause":
        painter.drawRoundedRect(QRectF(7.4, 4.2, 4.0, 15.6), 1.8, 1.8)
        painter.drawRoundedRect(QRectF(12.6, 4.2, 4.0, 15.6), 1.8, 1.8)
    elif kind == "prev":
        painter.drawRoundedRect(QRectF(5.3, 4.6, 3.0, 14.8), 1.5, 1.5)
        _triangle(painter, ((19.6, 4.6), (9.4, 12.0), (19.6, 19.4)))
    elif kind == "next":
        painter.drawRoundedRect(QRectF(15.7, 4.6, 3.0, 14.8), 1.5, 1.5)
        _triangle(painter, ((4.4, 4.6), (14.6, 12.0), (4.4, 19.4)))
    else:  # pragma: no cover - guards a typo instead of drawing nothing
        raise ValueError(f"unknown transport glyph: {kind}")

    return _finish(pixmap, painter)


def note_pixmap(size: int = 15, color: str | None = None) -> QPixmap:
    """One eighth note: the sidebar's marker for the library."""
    color = color or theme_color("ICON")
    pixmap, painter = _canvas(size)
    painter.setBrush(QColor(color))
    painter.drawEllipse(QPointF(9.1, 17.0), 4.1, 3.1)
    painter.drawRoundedRect(QRectF(11.9, 6.2, 2.4, 11.0), 1.2, 1.2)
    flag = QPainterPath()
    flag.moveTo(12.4, 6.6)
    flag.cubicTo(16.6, 7.6, 17.8, 10.4, 17.4, 13.4)
    flag.cubicTo(17.0, 10.9, 15.4, 9.4, 12.4, 9.2)
    flag.closeSubpath()
    painter.drawPath(flag)
    return _finish(pixmap, painter)


def queue_pixmap(size: int = 15, color: str | None = None) -> QPixmap:
    """Bulleted rows: the sidebar's marker for the playing queue."""
    color = color or theme_color("ICON")
    pixmap, painter = _canvas(size)
    painter.setBrush(QColor(color))
    for y, end in ((6.4, 19.4), (12.0, 19.4), (17.6, 15.6)):
        painter.drawEllipse(QPointF(5.4, y), 1.5, 1.5)
        painter.drawRoundedRect(QRectF(9.4, y - 1.15, end - 9.4, 2.3), 1.15, 1.15)
    return _finish(pixmap, painter)


def volume_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """Speaker cone with two waves: the volume strip's leading mark."""
    color = color or theme_color("ICON")
    pixmap, painter = _canvas(size)
    painter.setBrush(QColor(color))
    cone = QPainterPath()
    cone.moveTo(4.0, 9.4)
    cone.lineTo(7.4, 9.4)
    cone.lineTo(12.0, 4.8)
    cone.lineTo(12.0, 19.2)
    cone.lineTo(7.4, 14.6)
    cone.lineTo(4.0, 14.6)
    cone.closeSubpath()
    painter.drawPath(cone)

    painter.setBrush(Qt.BrushStyle.NoBrush)
    pen = QPen(QColor(color), 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawArc(QRectF(11.4, 8.2, 6.4, 7.6), -58 * 16, 116 * 16)
    painter.drawArc(QRectF(10.8, 5.4, 10.4, 13.2), -50 * 16, 100 * 16)
    return _finish(pixmap, painter)


def device_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """Headphones: the output device the transport is currently wired to."""
    color = color or theme_color("ICON")
    pixmap, painter = _canvas(size)
    pen = QPen(QColor(color), 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    band = QPainterPath()
    band.moveTo(4.9, 13.6)
    band.lineTo(4.9, 11.4)
    band.cubicTo(4.9, 7.1, 8.0, 4.5, 12.0, 4.5)
    band.cubicTo(16.0, 4.5, 19.1, 7.1, 19.1, 11.4)
    band.lineTo(19.1, 13.6)
    painter.drawPath(band)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawRoundedRect(QRectF(3.2, 12.8, 3.7, 6.8), 1.8, 1.8)
    painter.drawRoundedRect(QRectF(17.1, 12.8, 3.7, 6.8), 1.8, 1.8)
    return _finish(pixmap, painter)


def mode_pixmap(kind: str, size: int = 16, color: str | None = None) -> QPixmap:
    """``list`` (loop) / ``single`` (loop with one mark) / ``shuffle``."""
    color = color or theme_color("ICON")
    pixmap, painter = _canvas(size)
    pen = QPen(QColor(color), 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if kind in ("list", "single"):
        # One loop: the rail runs along the top to the right, down the right
        # edge, back along the bottom to the left and up again.
        painter.drawPath(_polyline((3.8, 11.0), (3.8, 8.4), (6.6, 5.0), (17.2, 5.0)))
        painter.drawPath(_polyline((15.0, 1.8), (18.4, 5.0), (15.0, 8.2)))
        painter.drawPath(_polyline((20.2, 13.0), (20.2, 15.6), (17.4, 19.0), (6.8, 19.0)))
        painter.drawPath(_polyline((9.0, 15.8), (5.6, 19.0), (9.0, 22.2)))
        if kind == "single":
            # A dot in the loop's middle. A "1" was tried first and turned to
            # mush at 16px: the digit has eight grid units of room and needs
            # more, so the loop says "repeat" and the dot says "one".
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(12.0, 12.0), 2.2, 2.2)
    elif kind == "shuffle":
        # Two tracks that cross, each leaving to the right through an arrow.
        painter.drawPath(_polyline((3.8, 7.0), (8.8, 7.0), (15.2, 17.0), (18.4, 17.0)))
        painter.drawPath(_polyline((15.8, 14.8), (18.6, 17.0), (15.8, 19.2)))
        painter.drawPath(_polyline((3.8, 17.0), (8.8, 17.0), (15.2, 7.0), (18.4, 7.0)))
        painter.drawPath(_polyline((15.8, 4.8), (18.6, 7.0), (15.8, 9.2)))
    else:  # pragma: no cover - guards a typo instead of drawing nothing
        raise ValueError(f"unknown mode glyph: {kind}")

    return _finish(pixmap, painter)


def _polyline(*points: tuple[float, float]) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(*points[0])
    for point in points[1:]:
        path.lineTo(*point)
    return path


def rounded_pixmap(source: QPixmap, size: int, radius: int) -> QPixmap:
    """Clip cover art to the rounded square the cover label draws.

    A QLabel ignores ``border-radius`` for its pixmap, so square art used to
    poke out of the rounded frame. The crop is taken from the centre and
    rendered at 2x to stay crisp on scaled displays.
    """
    scale = 2
    pixels = size * scale
    scaled = source.scaled(
        pixels,
        pixels,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    canvas = QPixmap(pixels, pixels)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    frame = QPainterPath()
    frame.addRoundedRect(QRectF(0, 0, pixels, pixels), radius * scale, radius * scale)
    painter.setClipPath(frame)
    painter.drawPixmap(
        int((pixels - scaled.width()) / 2),
        int((pixels - scaled.height()) / 2),
        scaled,
    )
    painter.end()
    canvas.setDevicePixelRatio(scale)
    return canvas


def plus_pixmap(size: int = 14, color: str | None = None) -> QPixmap:
    """A thin plus for "new" actions, drawn with round caps."""
    color = color or theme_color("ICON")
    pixmap, painter = _canvas(size)
    pen = QPen(QColor(color), 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawLine(QPointF(12, 6), QPointF(12, 18))
    painter.drawLine(QPointF(6, 12), QPointF(18, 12))
    return _finish(pixmap, painter)


def chevron_pixmap(
    direction: str = "left", size: int = 14, color: str | None = None
) -> QPixmap:
    """A bare angle bracket: the "back" mark on the search toggle."""
    color = color or theme_color("ICON")
    pixmap, painter = _canvas(size)
    pen = QPen(QColor(color), 2.1)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    if direction == "left":
        painter.drawPath(_polyline((15.0, 5.4), (8.6, 12.0), (15.0, 18.6)))
    else:
        painter.drawPath(_polyline((9.0, 5.4), (15.4, 12.0), (9.0, 18.6)))
    return _finish(pixmap, painter)
