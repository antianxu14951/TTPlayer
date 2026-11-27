"""Full-window background layer pinned ON TOP of every widget — images and video loop.

The layer is a plain QWidget raised above the whole interface via a stacked
layout. It is drawn with a low overall opacity, so the wallpaper's colour and
texture wash over the sidebar, table and player bar (including their white
surfaces) while all text stays readable. Mouse events pass straight through to
the UI below. An image is drawn at its original pixel size, centre-cropped to
the window; a video is decoded by QMediaPlayer and its current frame is drawn
the same way. Nothing is ever scaled, stretched or squashed. With no source set
the layer paints nothing.

Video runs with an infinite loop and no audio output: it is decoration, not a
second player competing with the music.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QWidget

# 背景层置顶，透明度统一固定：不提供用户自定义（设置里已删除该选项）。
# 20% 让背景明显可见，同时组件与文字保持清晰。
OVERLAY_OPACITY = 0.20

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".avi", ".mov", ".mkv", ".webm"}

_IMAGE_FILTER = "图片文件 (*.png *.jpg *.jpeg *.bmp *.webp *.gif)"
_VIDEO_FILTER = "视频文件 (*.mp4 *.m4v *.avi *.mov *.mkv *.webm)"
SUPPORTED_FORMATS = (
    f"图片或视频 (*.png *.jpg *.jpeg *.bmp *.webp *.gif "
    f"*.mp4 *.m4v *.avi *.mov *.mkv *.webm);;"
    f"{_IMAGE_FILTER};;{_VIDEO_FILTER};;所有文件 (*)"
)


def is_video(path: str | Path) -> bool:
    return Path(str(path)).suffix.lower() in VIDEO_EXTENSIONS


def _crop_pixmap(source: QPixmap, size: QSize) -> QPixmap | None:
    """按原始像素 1:1 居中裁剪出 ``size`` 区域，绝不缩放或拉伸。

    只从原图取与窗口等大的中心块；源图比窗口小的时候取整张图，留白区域
    不再用放大去填补，避免模糊和变形。
    """
    if source.isNull() or size.isEmpty():
        return None
    width = min(size.width(), source.width())
    height = min(size.height(), source.height())
    if width <= 0 or height <= 0:
        return None
    x = (source.width() - width) // 2
    y = (source.height() - height) // 2
    cropped = source.copy(x, y, width, height)
    return None if cropped.isNull() else cropped


def load_background_pixmap(path: str | Path, size: QSize) -> QPixmap | None:
    """Load an image file and centre-crop it at original pixel scale."""
    return _crop_pixmap(QPixmap(str(path)), size)


class BackgroundLayer(QWidget):
    """Paints the configured wallpaper (image or looping video) above all UI.

    The widget sits on top of the interface but draws at low opacity and lets
    every mouse event through, so it tints the whole window without stealing a
    single click.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 保存未裁剪的原图/视频帧；按当前尺寸裁剪放在 paintEvent 里做。
        # 这样构造时 widget 尚未布局（size 可能是 100x30）也不会把裁剪尺寸写死。
        self._source: QPixmap | None = None
        self._opacity: float = OVERLAY_OPACITY
        self._path: str = ""
        self._player = None  # QMediaPlayer, created lazily on first video
        self._sink = None  # QVideoSink
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # 置顶罩层必须让没有绘制到的区域完全透明，否则自身空白底会盖住组件。
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    # ---------- API ----------

    def has_background(self) -> bool:
        return bool(self._path)

    def background_path(self) -> str:
        return self._path

    def is_video_background(self) -> bool:
        return is_video(self._path)

    def opacity(self) -> float:
        """背景罩层固定不透明度（统一 20%，不提供自定义）。"""
        return self._opacity

    def set_opacity(self, value: float) -> None:
        """兼容旧调用：透明度已统一固定为 OVERLAY_OPACITY，忽略自定义值。"""
        self._opacity = OVERLAY_OPACITY
        self.update()

    def set_background(self, path: str | Path | None) -> bool:
        """Show ``path`` as background; returns True on success.

        An empty/None path clears the source. A missing file returns False and
        leaves the previous background in place. Videos loop forever, muted.
        """
        raw = str(path or "").strip()
        if not raw:
            self.clear()
            return True
        try:
            if not Path(raw).expanduser().is_file():
                return False
        except OSError:
            return False

        if is_video(raw):
            return self._start_video(raw)

        self._stop_video()
        source = QPixmap(str(Path(raw).expanduser()))
        if source.isNull():
            return False
        self._path = raw
        self._source = source
        self.update()
        return True

    def clear(self) -> None:
        self._stop_video()
        self._path = ""
        self._source = None
        self.update()

    def refresh(self) -> None:
        """重绘以适配新尺寸（裁剪本身在 paintEvent 中实时完成）。"""
        self.update()

    # ---------- video ----------

    def _start_video(self, raw: str) -> bool:
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

            if self._player is None:
                self._player = QMediaPlayer(self)
                self._sink = QVideoSink(self)
                self._player.setVideoSink(self._sink)
                # No audio output is set on purpose: a background video must
                # stay silent and never compete with the music player.
                self._sink.videoFrameChanged.connect(self._on_video_frame)
            self._player.setLoops(QMediaPlayer.Loops.Infinite)
            resolved = str(Path(raw).expanduser().resolve())
            self._player.setSource(QUrl.fromLocalFile(resolved))
            self._path = raw
            self._source = None  # first frame will arrive via the sink
            self._player.play()
            return True
        except Exception:
            return False

    def _stop_video(self) -> None:
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())

    def _on_video_frame(self, frame) -> None:  # noqa: ANN001 - QVideoFrame
        if not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._source = QPixmap.fromImage(image)
        self.update()

    # ---------- painting ----------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # opacity=0 / 无图时完全不绘制：本层透明，下面的组件原样显示。
        # 这是"透明度越低→组件越清晰"的数学保证——本层从不给组件染色。
        if self._source is None or self._opacity <= 0.0:
            return
        # 每次绘制都按当前窗口尺寸从原图 1:1 取中心块——不缩放、不拉伸，
        # 也避开了构造时 widget 尚未布局导致裁剪尺寸写死的问题。
        pixmap = _crop_pixmap(self._source, self.size())
        if pixmap is None:
            return
        x = (self.width() - pixmap.width()) // 2
        y = (self.height() - pixmap.height()) // 2
        painter = QPainter(self)
        # 只用最普通的 SourceOver：透明度只作用于这张背景图本身。
        # 不用任何颜色混合模式（Multiply/Screen 会把下层组件一起压暗/提亮），
        # 也不碰组件面板的 alpha——组件像素完全不参与本层的混合计算。
        painter.setOpacity(self._opacity)
        painter.drawPixmap(x, y, pixmap)
        painter.end()
