"""Export the TT播放器 logo to ``assets/`` as PNG, ICO and a brand sheet.

Run from the project root::

    .venv/Scripts/python tools/make_logo.py

The artwork itself lives in ``ui/branding.py`` so the exported files and the
in-app icon can never drift apart.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QTextOption,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.branding import (  # noqa: E402
    BRAND_LATIN,
    BRAND_NAME,
    BRAND_SLOGAN,
    COPYRIGHT_NOTICE,
    CREAM,
    INK,
    VERMILION,
    logo_image,
    paint_logo,
)

ASSETS = ROOT / "assets"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def write_ico(path: Path, sizes: tuple[int, ...] = ICO_SIZES) -> None:
    """Write a real multi-resolution .ico (PNG-compressed entries)."""
    entries: list[tuple[int, bytes]] = [
        (size, _png_bytes(logo_image(size))) for size in sorted(sizes)
    ]
    offset = 6 + 16 * len(entries)
    header = struct.pack("<HHH", 0, 1, len(entries))
    table = b""
    payload = b""
    for size, png in entries:
        side = 0 if size >= 256 else size
        table += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        payload += png
    path.write_bytes(header + table + payload)


def _font(size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(size)
    font.setWeight(weight)
    return font


def brand_sheet() -> QImage:
    width, height = 1200, 930
    sheet = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(QColor("#F3F3F0"))
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    # ---- header
    painter.save()
    painter.translate(64, 46)
    paint_logo(painter, 72)
    painter.restore()

    painter.setPen(QColor("#1d2230"))
    painter.setFont(_font(32, QFont.Weight.Bold))
    painter.drawText(QPointF(156, 84), BRAND_NAME)

    painter.setPen(QColor("#8b919c"))
    painter.setFont(_font(13))
    painter.drawText(QPointF(158, 110), f"{BRAND_SLOGAN} · 同一个字母，写两遍")

    # ---- light / dark stage
    for left, fill, caption in (
        (64, "#F7F6F1", "浅色背景"),
        (616, "#141925", "深色背景"),
    ):
        card = QRectF(left, 146, 520, 340)
        painter.setPen(QPen(QColor("#e6e6e1"), 1) if fill != "#141925" else Qt.PenStyle.NoPen)
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(card, 22, 22)
        painter.save()
        painter.translate(card.center().x() - 120, 166)
        paint_logo(painter, 240)
        painter.restore()
        painter.setPen(QColor("#9aa0aa") if fill != "#141925" else QColor("#7d8598"))
        painter.setFont(_font(12))
        painter.drawText(
            QRectF(card.left(), 428, card.width(), 22),
            Qt.AlignmentFlag.AlignHCenter,
            caption,
        )

    # ---- horizontal lockup
    lock = QRectF(64, 508, 1072, 128)
    painter.setPen(QPen(QColor("#e6e6e1"), 1))
    painter.setBrush(QColor("#ffffff"))
    painter.drawRoundedRect(lock, 20, 20)
    painter.save()
    painter.translate(104, 532)
    paint_logo(painter, 80)
    painter.restore()

    painter.setPen(QColor("#1d2230"))
    painter.setFont(_font(34, QFont.Weight.Bold))
    painter.drawText(QPointF(208, 578), BRAND_NAME)

    latin = _font(11)
    latin.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 5)
    painter.setFont(latin)
    painter.setPen(QColor("#8b919c"))
    painter.drawText(QPointF(211, 604), BRAND_LATIN)

    painter.setFont(_font(11))
    painter.setPen(QColor("#a2a8b3"))
    painter.drawText(
        QRectF(560, 546, 540, 40),
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        "组合标识：徽标 + 中文名 + 拉丁副名",
    )

    # ---- size ladder
    painter.setFont(_font(12, QFont.Weight.DemiBold))
    painter.setPen(QColor("#6f7580"))
    painter.drawText(QPointF(64, 684), "尺寸适配")

    x = 64
    for size in (16, 24, 32, 48, 64, 96):
        painter.drawImage(
            QRectF(x, 702 + (96 - size) / 2, size, size), logo_image(size)
        )
        painter.setFont(_font(11))
        painter.setPen(QColor("#a2a8b3"))
        painter.drawText(
            QRectF(x - 12, 806, size + 24, 16),
            Qt.AlignmentFlag.AlignHCenter,
            f"{size}px",
        )
        x += size + 46

    # ---- palette
    painter.setFont(_font(12, QFont.Weight.DemiBold))
    painter.setPen(QColor("#6f7580"))
    painter.drawText(QPointF(700, 684), "品牌色")

    swatches = (
        (INK, "墨 INK", "#0E111C"),
        (CREAM, "纸 PAPER", "#F4F0E5"),
        (VERMILION, "朱 VERMILION", "#F5513A"),
    )
    sx = 700
    for color, name, hex_code in swatches:
        painter.setPen(QPen(QColor(0, 0, 0, 46), 1))
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(QRectF(sx, 702, 74, 74), 14, 14)
        painter.setPen(QColor("#4a5060"))
        painter.setFont(_font(10))
        painter.drawText(QPointF(sx, 796), name)
        painter.setPen(QColor("#a2a8b3"))
        painter.drawText(QPointF(sx, 812), hex_code)
        sx += 138

    # ---- rationale + notice
    painter.setFont(_font(12))
    painter.setPen(QColor("#5d6472"))
    painter.drawText(
        QRectF(64, 852, 1072, 20),
        Qt.AlignmentFlag.AlignLeft,
        "概念：同一个字母写两遍 —— 墨色是静止的它，朱红是正在播放的它；圆形纸标签把两个 T 收进一张唱片。",
    )
    painter.setFont(_font(11))
    painter.setPen(QColor("#a2a8b3"))
    option = QTextOption(Qt.AlignmentFlag.AlignLeft)
    option.setWrapMode(QTextOption.WrapMode.WordWrap)
    painter.drawText(QRectF(64, 878, 1072, 36), COPYRIGHT_NOTICE, option)

    painter.end()
    return sheet


def main() -> None:
    app = QApplication(sys.argv)  # noqa: F841 - needed for font rendering
    ASSETS.mkdir(parents=True, exist_ok=True)

    logo_image(512).save(str(ASSETS / "logo.png"), "PNG")
    logo_image(256).save(str(ASSETS / "logo_256.png"), "PNG")
    write_ico(ASSETS / "logo.ico")
    brand_sheet().save(str(ASSETS / "logo_brand_sheet.png"), "PNG")

    for name in ("logo.png", "logo_256.png", "logo.ico", "logo_brand_sheet.png"):
        path = ASSETS / name
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
