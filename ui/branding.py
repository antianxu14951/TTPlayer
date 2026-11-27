"""Brand identity: the product name, the legal notice, and the logo mark.

Concept — **同一个字母，写两遍 / one letter, twice**
    ``TT`` stands on a cream disc, the abstracted label of a record. The first
    T is printed in ink: the player at rest. The second is vermilion: the same
    letter while it is playing. Same glyph, two states — which is also what a
    play button does. No music note, no play triangle: the name itself is the
    mark, and the disc is the only silhouette.

The mark is drawn with QPainter instead of shipped as a bitmap, so the window
icon, the sidebar emblem and the exported ``assets/logo.ico`` are the same
artwork at any DPI. ``tools/make_logo.py`` rasterises it for the assets folder.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)

BRAND_NAME = "TT播放器"
BRAND_SLOGAN = "让好音乐常伴左右"
BRAND_LATIN = "TT PLAYER"

COPYRIGHT_NOTICE = (
    "音乐平台不易，请尊重版权，支持正版。"
    "本项目仅供技术交流，不接受任何商业（包括但不限于广告等）合作及捐赠。"
    "禁止以任何方式售卖或转售代码以及打包文件。"
)

# Palette: ink ground, paper label, one loud accent for the "playing" T.
INK_TOP = "#1C2136"
INK_BOTTOM = "#07090F"
INK = "#0E111C"
PAPER_LIGHT = "#FCF9F1"
PAPER_DARK = "#EDE7D7"
CREAM = "#F4F0E5"
VERMILION = "#F5513A"
VERMILION_DEEP = "#D33C28"

# Glyph geometry, expressed on a 512 grid.
BAR_WIDTH = 148.0
STROKE = 60.0
CAP_HEIGHT = 196.0
GAP = 44.0
CAP_TOP = 158.0
CORNER = 6.0
DISC = 0.82


def _t_glyph(x: float, k: float) -> QPainterPath:
    """One T: crossbar plus stem, unioned so no seam shows through."""
    bar = QPainterPath()
    bar.addRoundedRect(
        QRectF(x * k, CAP_TOP * k, BAR_WIDTH * k, STROKE * k),
        CORNER * k,
        CORNER * k,
    )
    stem = QPainterPath()
    stem.addRoundedRect(
        QRectF(
            x * k + (BAR_WIDTH - STROKE) / 2 * k,
            CAP_TOP * k,
            STROKE * k,
            CAP_HEIGHT * k,
        ),
        CORNER * k,
        CORNER * k,
    )
    return bar.united(stem)


def paint_logo(painter: QPainter, size: float) -> None:
    """Draw the logo into a ``size`` x ``size`` box at the origin."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    k = size / 512.0

    body = QPainterPath()
    body.addRoundedRect(QRectF(0, 0, size, size), size * 0.245, size * 0.245)
    ground = QLinearGradient(0, 0, size * 0.5, size)
    ground.setColorAt(0.0, QColor(INK_TOP))
    ground.setColorAt(1.0, QColor(INK_BOTTOM))
    painter.fillPath(body, ground)

    painter.save()
    painter.setClipPath(body)
    glow = QRadialGradient(
        QPointF(size * 0.22, -size * 0.08), size * 1.15, QPointF(size * 0.22, -size * 0.08)
    )
    glow.setColorAt(0.0, QColor(255, 255, 255, 24))
    glow.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.fillRect(QRectF(0, 0, size, size), glow)

    # ---- paper label
    inset = (1 - DISC) / 2 * size
    disc = QPainterPath()
    disc.addEllipse(QRectF(inset, inset, size - 2 * inset, size - 2 * inset))
    paper = QRadialGradient(
        QPointF(size * 0.36, size * 0.34),
        DISC * size * 0.72,
        QPointF(size * 0.36, size * 0.34),
    )
    paper.setColorAt(0.0, QColor(PAPER_LIGHT))
    paper.setColorAt(1.0, QColor(PAPER_DARK))
    painter.fillPath(disc, paper)

    rim = QPen(QColor(14, 17, 28, 26), max(1.0, size * 0.006))
    painter.setPen(rim)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    edge = size * 0.012
    painter.drawEllipse(
        QRectF(inset + edge, inset + edge, size - 2 * inset - 2 * edge,
               size - 2 * inset - 2 * edge)
    )

    # ---- TT: same glyph twice, ink then vermilion
    total = BAR_WIDTH * 2 + GAP
    x0 = (512 - total) / 2
    painter.setPen(Qt.PenStyle.NoPen)
    painter.fillPath(_t_glyph(x0, k), QColor(INK))

    accent = QLinearGradient(0, CAP_TOP * k, 0, (CAP_TOP + CAP_HEIGHT) * k)
    accent.setColorAt(0.0, QColor(VERMILION))
    accent.setColorAt(1.0, QColor(VERMILION_DEEP))
    painter.fillPath(_t_glyph(x0 + BAR_WIDTH + GAP, k), accent)
    painter.restore()


def logo_image(size: int = 512) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    paint_logo(painter, float(size))
    painter.end()
    return image


def logo_pixmap(size: int = 46, device_pixel_ratio: float = 2.0) -> QPixmap:
    """Crisp emblem for the sidebar: rendered at 2x, downsampled by Qt."""
    pixel = max(1, int(round(size * device_pixel_ratio)))
    pixmap = QPixmap.fromImage(logo_image(pixel))
    pixmap.setDevicePixelRatio(device_pixel_ratio)
    return pixmap


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256, 512):
        icon.addPixmap(QPixmap.fromImage(logo_image(size)))
    return icon
