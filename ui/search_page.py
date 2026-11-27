"""The aggregated search view.

Sources are LX Music scripts shipped in ``sources/``; the player installs them
automatically, so this module only deals with searching and auditioning.

Search itself is done by the client (LX scripts never search), and a script is
only asked for a playable URL once the user previews a song or shelves it from
the row's context menu.

``SearchPage`` is a plain widget so the main window can host it as a peer of
the library view — searching never opens a window of its own.
"""
from __future__ import annotations

import time
import zlib
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import (
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    QThread,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from config.settings import load_config
from core.lx_proc import LxRuntimeError, LxScriptError, quality_name
from core.lx_source import (
    SEARCHABLE_PLATFORMS,
    LxStoreError,
    get_resolver,
    load_sources,
)
from core.models import Track
from core.music_source import prepare_audio, track_from_audio
from core.native_search import search_many
from ui.dialogs import NewPlaylistDialog
from ui.icons import note_pixmap
from ui.theme import theme_color, transparent_panel

QUALITY_CHOICES: tuple[tuple[str, str], ...] = (
    ("128k", "标准 128K"),
    ("320k", "高品 320K"),
    ("flac", "无损 FLAC"),
)


COVER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _short(text: str, limit: int = 42) -> str:
    value = (text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _cover_tint(hit: Any) -> QColor:
    """A stable cover tint derived from the song, never from its platform.

    Placeholder covers keep the grid lively, but they must not encode where a
    track came from — a hue per platform would give the source away.
    """
    seed = zlib.crc32(
        f"{getattr(hit, 'title', '')}|{getattr(hit, 'artist', '')}".encode("utf-8")
    )
    hue = 196 + seed % 62
    saturation = 96 + seed % 26
    value = 138 + seed % 28
    return QColor.fromHsv(hue, saturation, value)


def _mmss(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds)) // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def magnifier_pixmap(size: int = 18, color: str = "#a3aab6") -> QPixmap:
    """Small vector magnifier, crisper than any emoji or glyph font."""
    scale = 2
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(scale, scale)
    pen = QPen(QColor(color), 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QRectF(2.2, 2.2, size - 7.4, size - 7.4))
    painter.drawLine(
        QPointF(size - 6.2, size - 6.2), QPointF(size - 2.4, size - 2.4)
    )
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap


@dataclass
class _AddResult:
    """What the download worker hands back to the page."""

    track: Any
    hit: Any = None
    quality: str = ""
    playlist_id: str = ""
    new_playlist: str = ""


class _Task(QObject):
    """Run one callable off the UI thread; emit its result or error."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, func: Callable[[], Any]) -> None:
        super().__init__()
        self._func = func

    def run(self) -> None:
        try:
            self.finished.emit(self._func())
        except (LxScriptError, LxRuntimeError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self.failed.emit(f"{exc}")


class TaskRunner(QObject):
    """Owns a worker thread so the UI never blocks on script execution."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _Task | None = None
        self.on_success: Callable[[Any], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        self.on_done: Callable[[], None] | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, func: Callable[[], Any]) -> bool:
        if self.busy:
            return False
        self._thread = QThread()
        self._worker = _Task(func)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._handle_success)
        self._worker.failed.connect(self._handle_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)
        self._thread.start()
        return True

    def _handle_success(self, result: Any) -> None:
        if self.on_success:
            self.on_success(result)

    def _handle_error(self, message: str) -> None:
        if self.on_error:
            self.on_error(message)

    def _cleanup(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread = None
        self._worker = None
        if self.on_done:
            self.on_done()


# ---------------------------------------------------------------- workers


class SearchWorker(QObject):
    """Search every checked platform in parallel, off the UI thread."""

    finished = Signal(object)

    def __init__(self, platforms: list[str], keyword: str, limit: int, timeout: float) -> None:
        super().__init__()
        self._platforms = platforms
        self._keyword = keyword
        self._limit = limit
        self._timeout = timeout

    def run(self) -> None:
        hits, errors = search_many(
            self._platforms, self._keyword, self._limit, self._timeout
        )
        self.finished.emit({"hits": hits, "errors": errors})


class AddTrackWorker(QObject):
    """Resolve one hit, download it and hand back a Track.

    Library writes stay on the UI thread; this worker only talks to the
    network, ffmpeg and the tag reader, so playlist bookkeeping stays
    synchronous and predictable.
    """

    progress = Signal(str, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        hit: Any,
        quality: str,
        playlist_id: str = "",
        new_playlist: str = "",
    ) -> None:
        super().__init__()
        self._hit = hit
        self._quality = quality
        self._playlist_id = playlist_id
        self._new_playlist = new_playlist

    def run(self) -> None:
        hit = self._hit
        try:
            sources = [source for source in load_sources() if source.enabled]
            music_info = dict(hit.raw.get("music_info") or {})
            self.progress.emit("解析地址", hit.title)
            url, source, quality = get_resolver().resolve(
                sources, hit.source_id, self._quality, music_info, timeout=30
            )
            hit.audio_url = url
            hit.raw["resolved_by"] = source.name
            hit.raw["quality"] = quality
            hit.raw["resolved_source_id"] = source.id

            self.progress.emit("下载并转换", hit.title)
            path = prepare_audio(
                hit, lambda step, detail: self.progress.emit(step, detail)
            )
            self.finished.emit(
                _AddResult(
                    track_from_audio(hit, path),
                    hit,
                    quality,
                    self._playlist_id,
                    self._new_playlist,
                )
            )
        except Exception as exc:  # noqa: BLE001 - reported inline in the page
            self.failed.emit(f"{exc}")


# ---------------------------------------------------------------- covers


class CoverCache(QObject):
    """Fetch album art asynchronously and keep square thumbnails in memory.

    Uses ``QNetworkAccessManager`` rather than a thread pool: pending replies
    are cancelled with the page, so leaving it never waits on the network.
    """

    loaded = Signal(str, QImage)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pixmaps: dict[str, QPixmap] = {}
        self._failed: set[str] = set()
        self._pending: set[str] = set()
        self._manager = QNetworkAccessManager(self)
        self._manager.finished.connect(self._on_finished)

    def pixmap(self, url: str) -> QPixmap | None:
        if not url or url in self._failed:
            return None
        cached = self._pixmaps.get(url)
        if cached is not None:
            return cached
        if url not in self._pending:
            self._pending.add(url)
            request = QNetworkRequest(QUrl(url))
            request.setRawHeader(b"User-Agent", COVER_UA.encode("ascii"))
            request.setTransferTimeout(8000)
            request.setAttribute(
                QNetworkRequest.Attribute.RedirectPolicyAttribute,
                QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
            )
            self._manager.get(request)
        return None

    def _on_finished(self, reply: QNetworkReply) -> None:
        url = reply.url().toString()
        payload = b""
        if reply.error() == QNetworkReply.NetworkError.NoError:
            payload = bytes(reply.readAll())
        reply.deleteLater()
        self.store(url, QImage.fromData(payload) if payload else QImage())

    def store(self, url: str, image: QImage) -> None:
        self._pending.discard(url)
        if image.isNull():
            self._failed.add(url)
            return
        pixmap = QPixmap.fromImage(image)
        side = min(pixmap.width(), pixmap.height())
        if side <= 0:
            self._failed.add(url)
            return
        pixmap = pixmap.copy(
            (pixmap.width() - side) // 2, (pixmap.height() - side) // 2, side, side
        )
        self._pixmaps[url] = pixmap.scaled(
            96,
            96,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.loaded.emit(url, image)


# ---------------------------------------------------------------- song list


class _SongDelegate(QStyledItemDelegate):
    """Paint a song row: play glyph, cover, title and meta."""

    ROW_HEIGHT = 60

    def __init__(self, view: "SongList") -> None:
        super().__init__(view)
        self._view = view

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        return QSize(0, self.ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:  # noqa: N802
        view = self._view
        row = index.row()
        if not (0 <= row < len(view.hits)):
            return
        hit = view.hits[row]
        rect = option.rect
        hovered = view.hover_row == row
        active = view.active_row == row
        playing = view.playing_row == row

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        if playing or hovered or active:
            tone = theme_color("TINT") if playing else (theme_color("SURFACE_ALT") if hovered else theme_color("SURFACE_STRIPE"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tone))
            painter.drawRoundedRect(rect.adjusted(6, 2, -6, -2), 12, 12)

        if playing:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme_color("ACCENT")))
            painter.drawRoundedRect(
                QRectF(rect.left() + 9, rect.top() + 17, 3, rect.height() - 34), 1.5, 1.5
            )

        # Play glyph in place of the track number. The row button only starts
        # an audition - pausing belongs to the shared bottom transport, so the
        # icon never turns into a pause bar here.
        glyph = view.play_rect(rect)
        if hovered or playing:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme_color("ACCENT_DEEP") if playing else theme_color("ACCENT")))
            painter.drawEllipse(glyph)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#ffffff"))
            cx = glyph.center().x() + 0.5
            cy = glyph.center().y() + 0.5
            painter.drawPolygon(
                QPolygonF(
                    [
                        QPointF(cx - 3.0, cy - 5.2),
                        QPointF(cx - 3.0, cy + 5.2),
                        QPointF(cx + 4.8, cy),
                    ]
                )
            )
        else:
            number_font = QFont(view.font())
            number_font.setPixelSize(12)
            painter.setFont(number_font)
            painter.setPen(QColor(theme_color("FAINT")))
            painter.drawText(glyph, Qt.AlignmentFlag.AlignCenter, str(row + 1))

        # cover
        cover = view.cover_rect(rect)
        path = QPainterPath()
        path.addRoundedRect(QRectF(cover), 10, 10)
        painter.save()
        painter.setClipPath(path)
        pixmap = view.cover_pixmap(hit)
        if pixmap is not None:
            painter.drawPixmap(cover, pixmap)
        else:
            base = _cover_tint(hit)
            gradient = QLinearGradient(cover.topLeft(), cover.bottomRight())
            gradient.setColorAt(0.0, base.lighter(128))
            gradient.setColorAt(1.0, base.darker(112))
            painter.fillRect(cover, gradient)
            # Vector note, same drawing as the sidebar: a text glyph would pick
            # up whatever music symbol the local font happens to provide.
            note = note_pixmap(20, "#ffffff")
            note_side = note.width() / note.devicePixelRatio()
            painter.drawPixmap(
                int(cover.center().x() - note_side / 2),
                int(cover.center().y() - note_side / 2),
                note,
            )
        painter.restore()
        painter.setPen(QPen(QColor(20, 30, 50, 22), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        # title + meta
        text_left = cover.right() + 12
        text_right = rect.right() - 18 - 56 - 8
        width = max(60, text_right - text_left)
        title_font = QFont(view.font())
        title_font.setPixelSize(13)
        title_font.setWeight(QFont.Weight.DemiBold)
        metrics = QFontMetrics(title_font)
        title = metrics.elidedText(
            hit.title, Qt.TextElideMode.ElideRight, width
        )
        painter.setFont(title_font)
        painter.setPen(QColor(theme_color("ACCENT_DEEP")) if playing else QColor(theme_color("TEXT")))
        painter.drawText(
            QRect(text_left, rect.top() + 10, width, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            title,
        )

        meta_font = QFont(view.font())
        meta_font.setPixelSize(12)
        meta_font.setWeight(QFont.Weight.Normal)
        # The source is deliberately invisible: showing which platform a result
        # came from would put the app on the wrong side of a copyright notice.
        parts = [hit.artist, hit.album]
        meta = " · ".join(part for part in parts if part)
        meta = QFontMetrics(meta_font).elidedText(
            meta, Qt.TextElideMode.ElideRight, width
        )
        painter.setFont(meta_font)
        painter.setPen(QColor(theme_color("MUTED2")))
        painter.drawText(
            QRect(text_left, rect.top() + 31, width, 18),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            meta,
        )

        # duration
        duration_font = QFont(view.font())
        duration_font.setPixelSize(12)
        painter.setFont(duration_font)
        painter.setPen(QColor(theme_color("ACCENT") if playing else theme_color("FAINT")))
        painter.drawText(
            QRect(rect.right() - 18 - 56, rect.top(), 56, rect.height()),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "试听中" if playing else (_mmss(hit.duration_ms) if hit.duration_ms else "--:--"),
        )
        painter.restore()


class SongList(QListWidget):
    """A borderless song list with hover preview and a context menu."""

    previewRequested = Signal(int)
    menuRequested = Signal(int, QPoint)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("songList")
        self.hits: list[Any] = []
        self.hover_row = -1
        self.active_row = -1
        self.playing_row = -1
        self._covers = CoverCache(self)
        self._covers.loaded.connect(self._on_cover_ready)
        self.setItemDelegate(_SongDelegate(self))
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_menu)

    # ---------------------------------------------------------- geometry

    @staticmethod
    def play_rect(rect: QRect) -> QRect:
        return QRect(rect.left() + 14, rect.top() + (rect.height() - 26) // 2, 26, 26)

    @staticmethod
    def cover_rect(rect: QRect) -> QRect:
        return QRect(rect.left() + 52, rect.top() + (rect.height() - 42) // 2, 42, 42)

    # ---------------------------------------------------------- data

    def set_hits(self, hits: list[Any]) -> None:
        self.clear()
        self.hits = list(hits)
        self.hover_row = -1
        self.active_row = -1
        self.playing_row = -1
        for _ in self.hits:
            item = QListWidgetItem()
            item.setSizeHint(QSize(0, _SongDelegate.ROW_HEIGHT))
            self.addItem(item)
        # No current row: keyboard navigation uses active_row, and Qt would
        # otherwise paint a dotted focus rectangle over the last row.
        self.setCurrentRow(-1)
        self.viewport().update()

    def cover_pixmap(self, hit: Any) -> QPixmap | None:
        return self._covers.pixmap(str(getattr(hit, "cover_url", "") or ""))

    def _on_cover_ready(self, url: str, image: QImage) -> None:
        # The thumbnail is already stored; just repaint the visible rows.
        self.viewport().update()

    # ---------------------------------------------------------- interaction

    def _row_at(self, pos: QPoint) -> int:
        item = self.itemAt(pos)
        return self.row(item) if item is not None else -1

    def _row_rect(self, row: int) -> QRect:
        item = self.item(row)
        return self.visualItemRect(item) if item is not None else QRect()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        row = self._row_at(event.position().toPoint())
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self.hover_row != -1:
            self.hover_row = -1
            self.viewport().update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        row = self._row_at(pos)
        if row < 0:
            return
        rect = self._row_rect(row)
        if self.play_rect(rect).contains(pos):
            self.previewRequested.emit(row)
            return
        if self.active_row != row:
            self.active_row = row
            self.setCurrentRow(-1)
            self.viewport().update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        row = self._row_at(pos)
        if row < 0:
            return
        self.previewRequested.emit(row)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self.hits:
            step = 1 if key == Qt.Key.Key_Down else -1
            row = max(0, min(len(self.hits) - 1, self.active_row + step))
            self.active_row = row
            self.scrollToItem(self.item(row))
            self.viewport().update()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.active_row >= 0:
            self.previewRequested.emit(self.active_row)
            return
        super().keyPressEvent(event)

    def _on_menu(self, pos: QPoint) -> None:
        row = self._row_at(pos)
        if row >= 0:
            self.menuRequested.emit(row, pos)


# ---------------------------------------------------------------- quality


class QualityButton(QPushButton):
    """A pill that opens the quality menu, instead of a native combo box."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("qualityButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(132)
        self._value = ""
        self._menu = QMenu(self)
        self._actions: dict[str, Any] = {}
        for value, label in QUALITY_CHOICES:
            action = self._menu.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(
                lambda _checked=False, key=value: self.set_value(key)
            )
            self._actions[value] = action
        self.clicked.connect(self._popup)
        self.set_value("320k")

    def currentData(self) -> str:  # noqa: N802 - mirrors QComboBox
        return self._value

    def set_value(self, value: str) -> None:
        if value not in self._actions:
            value = "320k"
        self._value = value
        for key, action in self._actions.items():
            action.setChecked(key == value)
        self.setText(dict(QUALITY_CHOICES)[value])
        self.setToolTip(f"试听与导入音质：{dict(QUALITY_CHOICES)[value]}")

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(theme_color("MUTED2")), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        center_x = self.width() - 15
        center_y = self.height() / 2 + 1
        painter.drawLine(
            QPointF(center_x - 4, center_y - 2), QPointF(center_x, center_y + 2)
        )
        painter.drawLine(
            QPointF(center_x, center_y + 2), QPointF(center_x + 4, center_y - 2)
        )
        painter.end()

    def _popup(self) -> None:
        self._menu.exec(self.mapToGlobal(QPoint(0, self.height() + 4)))


# ---------------------------------------------------------------- page


class SearchPage(QWidget):
    """Search every available source, audition, and shelve what you keep."""

    imported = Signal(int)
    playlistsChanged = Signal()
    # True once there are rows worth showing, False when the panel is back to
    # its hint. The main window uses it to grow or shrink this half of the
    # split screen, so the playlist keeps the space while the search is idle.
    resultsChanged = Signal(bool)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        player: Any = None,
        library: Any = None,
        search_edit: QLineEdit | None = None,
        search_button: QPushButton | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("searchPage")
        self.library = library
        self._main_player = player
        # The keyword field and the search key live in the main window's
        # topbar; the topbar triggers the search, this page only renders the
        # outcome.
        self.keyword_edit = search_edit
        self.search_button = search_button
        self._active = True
        self._hits: list[Any] = []
        self._platforms: list[str] = []
        self._search_thread: QThread | None = None
        self._search_worker: SearchWorker | None = None
        self._add_thread: QThread | None = None
        self._add_worker: AddTrackWorker | None = None
        self._adding = False
        self._preview_row = -1
        # Resolved stream URLs, keyed by a stable hit key. Auditioning is
        # handled by the shared transport (see ``_start_preview``) so the page
        # no longer owns a player, a progress bar or transport buttons.
        self._preview_urls: dict[str, str] = {}
        # Every resolve carries the generation it was started under. When the
        # user clicks another song the generation moves on; the abandoned
        # resolve keeps running in its own thread but its outcome only lands
        # in the URL cache, never on the transport.
        self._preview_generation = 0
        self._preview_runner = TaskRunner(self)
        self._build_ui()
        if self._main_player is not None:
            self._main_player.previewChanged.connect(self._on_preview_changed)
            self._main_player.previewNextRequested.connect(
                lambda: self._preview_step(1)
            )
            self._main_player.previewPreviousRequested.connect(
                lambda: self._preview_step(-1)
            )
        self.refresh_sources()

    # ------------------------------------------------------------ layout

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        # The main window owns the outer margins, so the page starts flush.
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # One card for the whole panel: results and status. The keyword field
        # and the search key live in the topbar now, so the card only carries
        # what a search produces.
        card = QFrame()
        card.setObjectName("searchCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 6)
        card_layout.setSpacing(8)

        self.stack = QStackedWidget()
        # The page fills the whole content slot, but a list's minimumSizeHint
        # would still fight the footer at small sizes; ignored height keeps
        # the layout honest.
        self.stack.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored
        )
        self.stack.addWidget(self._build_empty_page())
        self.results = SongList()
        self.results.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored
        )
        self.stack.addWidget(self.results)
        card_layout.addWidget(self.stack, stretch=1)

        footer = QHBoxLayout()
        footer.setSpacing(12)
        self.count_label = QLabel("尚未搜索")
        self.count_label.setObjectName("listCount")
        footer.addWidget(self.count_label)
        self.status_label = QLabel("")
        self.status_label.setObjectName("searchStatus")
        footer.addWidget(self.status_label, stretch=1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("importProgressBar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedWidth(140)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setVisible(False)
        footer.addWidget(self.progress_bar, alignment=Qt.AlignmentFlag.AlignVCenter)
        # Quality affects auditioning and importing; chosen once, glanced at
        # rarely, so it rides at the end of the status line.
        self.quality_combo = QualityButton()
        footer.addWidget(self.quality_combo, alignment=Qt.AlignmentFlag.AlignVCenter)
        card_layout.addLayout(footer)

        root.addWidget(card)

        self.results.previewRequested.connect(self._start_preview)
        self.results.menuRequested.connect(self._on_list_menu)

    def _build_empty_page(self) -> QWidget:
        """A single quiet line: the panel is short until there is something in it."""
        page = QWidget()
        page.setObjectName("searchEmpty")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(0)
        layout.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        badge = QLabel()
        badge.setPixmap(note_pixmap(18, theme_color("FAINT")))
        row.addWidget(badge)
        text_box = transparent_panel(QWidget())
        text_layout = QVBoxLayout(text_box)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(1)
        self.empty_title = QLabel("搜索你想听的歌")
        self.empty_title.setObjectName("emptyTitleSmall")
        self.empty_hint = QLabel("同时检索全部来源 · 双击试听 · 右键下载并加入播放列表或歌单")
        self.empty_hint.setObjectName("searchMeta")
        text_layout.addWidget(self.empty_title)
        text_layout.addWidget(self.empty_hint)
        row.addWidget(text_box)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def _show_empty(self, title: str, hint: str) -> None:
        self.empty_title.setText(title)
        self.empty_hint.setText(hint)
        self.stack.setCurrentIndex(0)
        self.resultsChanged.emit(False)

    # ------------------------------------------------------------ sources

    def refresh_sources(self) -> None:
        """Re-read the installed-source list from SQLite.

        The ``sources/`` folders are scanned at every app launch; newly dropped
        files therefore become available on the next start. Every available
        source is used on every search — there is deliberately no platform
        picker, because naming the platforms under the results is what gets a
        project like this into trouble.
        """
        try:
            sources = [source for source in load_sources() if source.enabled]
        except LxStoreError:
            sources = []
        supported: set[str] = set()
        for source in sources:
            supported.update(source.searchable_platforms())
        self._platforms = [key for key in SEARCHABLE_PLATFORMS if key in supported]
        if not self._platforms and not self._hits:
            self._show_empty(
                "还没有可用音源",
                "把音源脚本（.js）放进程序目录下的 sources 文件夹，下次启动时会自动加载。",
            )

    # ------------------------------------------------------------ lifecycle

    def set_active(self, active: bool) -> None:
        """Called by the main window when this view is entered or left."""
        self._active = active
        if active:
            self.refresh_sources()
            self.keyword_edit.setFocus()
        else:
            self.stop_preview()

    def stop_preview(self) -> None:
        """Hand the transport back to the library's queue.

        The main window calls this when leaving the search view, so the bottom
        bar always ends up showing the real playlist again instead of a stream
        that no longer belongs to any list.
        """
        # Any resolve still in flight belongs to a song nobody is watching
        # anymore: let it finish for the cache, never for playback.
        self._preview_generation += 1
        if self._main_player is not None:
            self._main_player.exit_preview()
        self._clear_preview_marks()

    def _clear_preview_marks(self) -> None:
        self._preview_row = -1
        self.results.playing_row = -1
        self.results.viewport().update()

    def _on_preview_changed(self, active: bool) -> None:
        if not active:
            self._clear_preview_marks()

    def search_hint(self, message: str) -> None:
        """Explain a blocked action in place instead of raising a message box."""
        self.status_label.setText(message)

    # ------------------------------------------------------------ search

    def _on_search(self) -> None:
        keyword = self.keyword_edit.text().strip()
        if not keyword:
            self.search_hint("请输入歌曲名或歌手")
            self.keyword_edit.setFocus()
            return
        if self._search_thread is not None and self._search_thread.isRunning():
            return
        # The panel is always on screen, so a source installed while the app
        # was running never passed through an "entering the page" hook; pick it
        # up here instead.
        self.refresh_sources()
        platforms = list(self._platforms)
        if not platforms:
            self.refresh_sources()
            self.search_hint("还没有可用音源，请稍后再试")
            return

        config = load_config()
        limit = int(config.get("search_limit_per_source", 30))
        timeout = float(config.get("search_timeout_seconds", 15))

        self.search_button.setEnabled(False)
        self.search_button.setText("搜索中…")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.count_label.setText("正在搜索…")
        self.status_label.setText("")
        # A new query replaces the result list, so the audition must end and
        # the transport goes back to the library - paused, so searching does
        # not suddenly start the previous playlist.
        if self._main_player is not None:
            self._main_player.exit_preview(resume=False)
        self._clear_preview_marks()

        self._search_thread = QThread()
        self._search_worker = SearchWorker(platforms, keyword, limit, timeout)
        self._search_worker.moveToThread(self._search_thread)
        self._search_thread.started.connect(self._search_worker.run)
        self._search_worker.finished.connect(self._on_search_finished)
        self._search_worker.finished.connect(self._search_thread.quit)
        self._search_thread.finished.connect(self._search_worker.deleteLater)
        # Drop the Python reference before the C++ object is scheduled for
        # deletion: a second search then sees None instead of a dead wrapper,
        # and isRunning() never touches deleted memory.
        self._search_thread.finished.connect(self._release_search_thread)
        self._search_thread.finished.connect(self._search_thread.deleteLater)
        self._search_thread.start()

    def _release_search_thread(self) -> None:
        self._search_thread = None
        self._search_worker = None

    def _on_search_finished(self, payload: dict[str, Any]) -> None:
        self.search_button.setEnabled(True)
        self.search_button.setText("搜索")
        self.progress_bar.setVisible(False)
        self._hits = list(payload.get("hits") or [])
        self.results.set_hits(self._hits)
        if self._hits:
            self.stack.setCurrentIndex(1)
            self.count_label.setText(f"共 {len(self._hits)} 首")
            self.resultsChanged.emit(True)
        else:
            self.count_label.setText("没有找到")
            self._show_empty("没有找到相关歌曲", "换个关键词试试，或者稍后再搜索。")
        errors = payload.get("errors") or []
        self.status_label.setText("；".join(errors))

    # ------------------------------------------------------------ context menu

    def _row_menu(self, row: int) -> tuple[QMenu, Any, Any, Any]:
        """Build the row menu and return it with its three own actions."""
        menu = QMenu(self)
        preview_action = menu.addAction("试听这首")
        menu.addSeparator()
        queue_action = menu.addAction("添加到播放列表")
        queue_action.setEnabled(self._main_player is not None)
        playlist_menu = menu.addMenu("添加到歌单")
        playlists = list(self.library.ordered_playlists()) if self.library else []
        for playlist in playlists:
            action = playlist_menu.addAction(playlist.name)
            action.triggered.connect(
                lambda _checked=False, pid=playlist.id: self._download_and_add(row, pid)
            )
        if playlists:
            playlist_menu.addSeparator()
        new_action = playlist_menu.addAction("新建歌单并添加…")
        new_action.setEnabled(self.library is not None)
        playlist_menu.setEnabled(self.library is not None)
        return menu, preview_action, queue_action, new_action

    def _on_list_menu(self, row: int, pos: QPoint) -> None:
        """Right-click is the only way to keep a song: it downloads on demand.

        There is no checkbox and no bulk button — a song is only fetched when
        the user actually says where it should go.
        """
        if not (0 <= row < len(self._hits)):
            return
        menu, preview_action, queue_action, new_action = self._row_menu(row)
        chosen = menu.exec(self.results.viewport().mapToGlobal(pos))
        if chosen is preview_action:
            self._start_preview(row)
        elif chosen is queue_action:
            self._download_and_add(row, "")
        elif chosen is new_action:
            self._add_to_new_playlist(row)

    def _add_to_new_playlist(self, row: int) -> None:
        if self.library is None:
            return
        dialog = NewPlaylistDialog(self)
        if dialog.exec() != NewPlaylistDialog.DialogCode.Accepted:
            return
        # The playlist is created only after the download succeeds, so a
        # failed download never leaves an empty list behind.
        self._download_and_add(row, new_playlist=dialog.name)

    # ------------------------------------------------------------ download & shelve

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join((text or "").split()).casefold()

    def _existing_track(self, hit: Any) -> Any:
        """Return an already imported copy, so repeat adds never re-download."""
        if self.library is None:
            return None
        title = self._norm(hit.title)
        artist = self._norm(hit.artist)
        if not title:
            return None
        for track in self.library.tracks.values():
            if self._norm(track.title) != title:
                continue
            if artist and self._norm(track.artist) != artist:
                continue
            return track
        return None

    def _download_and_add(
        self, row: int, playlist_id: str = "", new_playlist: str = ""
    ) -> None:
        """Resolve and download one hit, then shelve it in a queue or playlist."""
        if not (0 <= row < len(self._hits)):
            return
        if self.library is None:
            self.search_hint("媒体库尚未就绪，暂时无法下载")
            return
        if self._adding:
            self.search_hint("正在下载上一首，请稍候…")
            return

        hit = self._hits[row]
        existing = self._existing_track(hit)
        if existing is not None:
            self._shelve(existing, playlist_id, new_playlist)
            return

        quality = self.quality_combo.currentData() or "320k"
        self._adding = True
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.status_label.setText(f"正在下载《{hit.title}》…")

        thread = QThread()
        worker = AddTrackWorker(hit, quality, playlist_id, new_playlist)
        self._add_thread = thread
        self._add_worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_add_progress)
        worker.finished.connect(self._on_add_finished)
        worker.failed.connect(self._on_add_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._release_add_thread)
        thread.start()

    def _on_add_progress(self, step: str, detail: str) -> None:
        self.status_label.setText(f"{step}：《{detail}》")

    def _on_add_finished(self, result: Any) -> None:
        self.progress_bar.setVisible(False)
        if not isinstance(result, _AddResult):
            return
        self._shelve(result.track, result.playlist_id, result.new_playlist)

    def _on_add_failed(self, message: str) -> None:
        self._adding = False
        self.progress_bar.setVisible(False)
        self.status_label.setText(f"下载失败：{_short(message, 80)}")

    def _release_add_thread(self) -> None:
        self._add_thread = None
        self._add_worker = None
        self._adding = False

    def _shelve(
        self, track: Any, playlist_id: str = "", new_playlist: str = ""
    ) -> None:
        """Store the track in the library, then in a playlist or the queue."""
        added = False
        if self.library is not None:
            added = self.library.add_track(track)
            if not added:
                same_file = next(
                    (
                        candidate
                        for candidate in self.library.tracks.values()
                        if candidate.path == track.path
                    ),
                    None,
                )
                track = same_file or track
        if added:
            self.imported.emit(1)
        self.status_label.setToolTip("")

        if new_playlist and self.library is not None:
            playlist_id = self.library.create_playlist(new_playlist).id
            self.playlistsChanged.emit()

        if playlist_id:
            playlist = (
                self.library.playlists.get(playlist_id) if self.library else None
            )
            if playlist is None:
                self.status_label.setText("歌单已不存在")
                return
            count = self.library.add_to_playlist(playlist_id, [track.id])
            self.playlistsChanged.emit()
            if count:
                self.status_label.setText(
                    f"已下载并加入歌单「{playlist.name}」：《{track.title}》"
                )
            else:
                self.status_label.setText(f"《{track.title}》已经在歌单里了")
            return

        queued = (
            self._main_player.enqueue([track]) if self._main_player is not None else 0
        )
        # Queueing also downloads the file, and a downloaded file that belongs
        # to no playlist would be invisible in a playlist-only library. The
        # default bucket catches it, exactly like an import without a target.
        filed = False
        if self.library is not None:
            default_id = self.library.default_playlist_id()
            filed = bool(self.library.add_to_playlist(default_id, [track.id]))
            if filed:
                self.playlistsChanged.emit()
        if queued and filed:
            self.status_label.setText(
                f"已下载并加入播放列表，同时归入「默认歌单」：《{track.title}》"
            )
        elif queued:
            self.status_label.setText(f"已下载并加入播放列表：《{track.title}》")
        elif filed:
            self.status_label.setText(f"已下载并归入「默认歌单」：《{track.title}》")
        else:
            self.status_label.setText(f"《{track.title}》已经在播放列表中")

    # ------------------------------------------------------------ preview

    @staticmethod
    def _hit_key(hit: Any) -> str:
        raw_id = str(getattr(hit, "id", "") or "")
        return f"{getattr(hit, 'source_id', '')}|{raw_id or hit.title}|{hit.artist}"

    @staticmethod
    def _preview_track(hit: Any) -> Track:
        """Wrap a search hit as a throw-away Track for the shared transport.

        The ``preview:`` prefix keeps these ids out of the library namespace,
        so the library table never highlights a stream as an imported file.
        """
        raw_id = str(getattr(hit, "id", "") or "")
        return Track(
            id=f"preview:{getattr(hit, 'source_id', '')}:{raw_id or hit.title}",
            path="",
            title=hit.title,
            artist=hit.artist,
            album=hit.album,
            duration_ms=hit.duration_ms,
            source="preview",
        )

    def _start_preview(self, row: int) -> None:
        if not (0 <= row < len(self._hits)):
            return
        if self._main_player is None:
            self.search_hint("播放器尚未就绪，暂时无法试听")
            return
        hit = self._hits[row]
        tracks = [self._preview_track(candidate) for candidate in self._hits]
        self._preview_row = row
        self.results.playing_row = row
        self.results.viewport().update()

        cached = self._preview_urls.get(self._hit_key(hit), "")
        if cached:
            self._main_player.start_preview(tracks, row, cached)
            self.status_label.setText(f"试听中 · 《{hit.title}》")
            return

        quality = self.quality_combo.currentData() or "320k"
        # Show the song on the bottom bar right away, paused, so the click has
        # immediate feedback while the script resolves a stream URL.
        self._main_player.prepare_preview(tracks, row)
        self.status_label.setText(f"正在解析《{hit.title}》…")
        key = self._hit_key(hit)
        self._preview_generation += 1
        generation = self._preview_generation
        if self._preview_runner.busy:
            # A slow resolve must never hold the next click hostage: leave it
            # running in its own thread — its result will be cache-only — and
            # give the new request a runner of its own.
            self._preview_runner = TaskRunner(self)
        self._preview_runner.on_success = (
            lambda result, gen=generation: self._on_preview_resolved(result, gen)
        )
        self._preview_runner.on_error = (
            lambda message, gen=generation: self._on_preview_failed(message, gen)
        )
        self._preview_runner.start(
            lambda: self._resolve_for_preview(hit, quality, row, key, generation)
        )

    @staticmethod
    def _resolve_for_preview(
        hit: Any, quality: str, row: int, key: str, generation: int = 0
    ) -> dict[str, Any]:
        sources = [source for source in load_sources() if source.enabled]
        music_info = dict(hit.raw.get("music_info") or {})
        started = time.time()
        url, _source, used = get_resolver().resolve(
            sources, hit.source_id, quality, music_info, timeout=25
        )
        return {
            "row": row,
            "key": key,
            "title": hit.title,
            "url": url,
            "quality": used,
            "elapsed": time.time() - started,
            "generation": generation,
        }

    def _on_preview_resolved(self, result: Any, generation: int = 0) -> None:
        if not self._active or not isinstance(result, dict):
            return
        row = int(result.get("row", -1))
        url = str(result.get("url") or "")
        # Whatever the generation, a resolved URL is worth keeping: the user
        # may click back to that song later and skip the resolve entirely.
        if url:
            self._preview_urls[str(result.get("key") or "")] = url
        # A stale resolve (the user clicked another song, or searched again)
        # must not steal the transport — its song was already abandoned.
        if generation != self._preview_generation:
            return
        # A new search may have replaced the list while this was resolving.
        if row != self._preview_row or not (0 <= row < len(self._hits)) or not url:
            return
        tracks = [self._preview_track(candidate) for candidate in self._hits]
        if self._main_player is not None:
            self._main_player.start_preview(tracks, row, url)
        self.results.playing_row = row
        self.results.viewport().update()
        self.status_label.setText(
            f"试听中 · {quality_name(str(result.get('quality') or ''))} · "
            f"{float(result.get('elapsed') or 0):.1f} 秒解析"
        )

    def _preview_step(self, delta: int) -> None:
        """Move inside the temporary result list when next/previous is used."""
        if not self._active:
            return
        target = self._preview_row + delta
        if 0 <= target < len(self._hits):
            self._start_preview(target)

    def _on_preview_failed(self, message: str, generation: int = 0) -> None:
        if not self._active:
            return
        # The user moved on to another song; the abandoned resolve's failure
        # is not this song's failure and must not touch the transport.
        if generation != self._preview_generation:
            return
        self.status_label.setText(f"试听失败：{message}")
        if self._main_player is not None:
            # Put the library's own track back on the transport instead of
            # leaving a stream that never resolved.
            self._main_player.exit_preview()

    # ------------------------------------------------------------ close

    def shutdown(self) -> None:
        """Release the transport before the window goes away.

        A resolve that finishes later must not start playback either, so the
        page is marked inactive first.
        """
        self._active = False
        self._preview_generation += 1
        if self._main_player is not None:
            self._main_player.exit_preview(restore=False)
        self._clear_preview_marks()
