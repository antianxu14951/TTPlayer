from __future__ import annotations

import threading
import time
from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QSize, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QSlider,
    QStackedWidget,
    QStyle,
    QStyleOptionSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config.settings import (
    COVER_DIR,
    current_db_file,
    load_config,
    load_playback_state,
    save_config,
    switch_database_file,
)
from core.douyin import DouyinImportResult
from core.library import Library
from core.metadata import build_track_from_file
from core.models import Track
from core.player import MODE_LABELS, PlaybackMode, PlayerController
from ui.background import SUPPORTED_FORMATS, BackgroundLayer
from ui.branding import (
    BRAND_NAME,
    BRAND_SLOGAN,
    COPYRIGHT_NOTICE,
    app_icon,
    logo_pixmap,
)
from ui.formatting import format_duration
from ui.dialogs import (
    DouyinImportDialog,
    NewPlaylistDialog,
    PlaylistPickerDialog,
    SettingsDialog,
    ask_rename,
)
from ui.icons import (
    device_pixmap,
    mode_pixmap,
    note_pixmap,
    plus_pixmap,
    queue_pixmap,
    rounded_pixmap,
    transport_pixmap,
    volume_pixmap,
)
from ui.queue_popup import QueuePopup
from ui.texts import ask_confirm, ask_danger, show_info, warning
from ui.search_page import SearchPage, magnifier_pixmap
from version import version_string
from ui.theme import (
    current_theme_id,
    install_button_cursors,
    set_background_active,
    theme_color,
    transparent_panel,
)

MODE_ORDER = [PlaybackMode.LIST_LOOP, PlaybackMode.SHUFFLE, PlaybackMode.SINGLE_LOOP]

MODE_TOOLTIPS = {
    PlaybackMode.LIST_LOOP: "列表循环：按当前列表顺序播放，播完最后一首回到第一首",
    PlaybackMode.SHUFFLE: "随机播放：随机选择未播放曲目，尽量避开刚播放过的音乐",
    PlaybackMode.SINGLE_LOOP: "单曲循环：当前曲目播完后自动从头重新播放",
}

# One glyph per mode, drawn on the same grid as the rest of the transport.
MODE_ICONS = {
    PlaybackMode.LIST_LOOP: "list",
    PlaybackMode.SHUFFLE: "shuffle",
    PlaybackMode.SINGLE_LOOP: "single",
}

AUDIO_FILTER = "音频文件 (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.wma *.opus);;所有文件 (*)"

# Player bar geometry. The bar is one fixed canvas, so these are the numbers
# the three columns are balanced against rather than a flexible grid.
BAR_HEIGHT = 104
COVER_SIZE = 56
INFO_WIDTH = 232
INFO_TEXT_WIDTH = INFO_WIDTH - COVER_SIZE - 12
TIME_WIDTH = 36
VOLUME_WIDTH = 76
UTILITY_KEY = 34
QUEUE_BOX = 40


class ClickableSlider(QSlider):
    """Horizontal slider where clicking the groove jumps to that exact value.

    Dragging the handle keeps Qt's native behaviour. Pressing the groove sets
    the value to the clicked point immediately (which also fires ``valueChanged``
    so connected handlers apply it); moving while the press is held scrubs
    continuously. Subclasses may override :meth:`_on_groove_value` to add a
    side effect - :class:`SeekSlider` emits its own ``seeked`` signal there.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._groove_pressed = False

    def _on_groove_value(self, value: int) -> None:
        """Apply a value chosen by a groove click/drag (default: just set it)."""
        if value != self.value():
            self.setValue(value)

    def _handle_length(self) -> int:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        return max(
            8,
            self.style().pixelMetric(
                QStyle.PixelMetric.PM_SliderLength, option, self
            ),
        )

    def _value_at(self, x: float) -> int:
        handle = self._handle_length()
        span = max(1, self.width() - handle)
        position = int(x - handle / 2)
        value = QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), position, span
        )
        return min(self.maximum(), max(self.minimum(), value))

    def _is_on_handle(self, x: float) -> bool:
        if self.maximum() <= self.minimum():
            return True
        handle = self._handle_length()
        span = max(1, self.width() - handle)
        ratio = (self.value() - self.minimum()) / (self.maximum() - self.minimum())
        center = handle / 2 + ratio * span
        return abs(x - center) <= handle / 2 + 2

    def mousePressEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self._is_on_handle(event.position().x())
            and self.maximum() > self.minimum()
        ):
            self._groove_pressed = True
            self._on_groove_value(self._value_at(event.position().x()))
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._groove_pressed:
            self._on_groove_value(self._value_at(event.position().x()))
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._groove_pressed and event.button() == Qt.MouseButton.LeftButton:
            self._groove_pressed = False
            return
        super().mouseReleaseEvent(event)


class SeekSlider(ClickableSlider):
    """进度条：点击跳转时额外发出 ``seeked`` 供播放器定位。"""

    seeked = Signal(int)

    def _on_groove_value(self, value: int) -> None:
        if value != self.value():
            self.setValue(value)
        self.seeked.emit(value)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.library = Library()
        self.player = PlayerController(self)
        self._view_playlist_id = ""
        self._displayed_tracks: list[Track] = []
        self._slider_dragging = False
        self._restoring = False
        # Qt.Popup closes itself when the user presses anywhere outside it,
        # including the key that opened it; this marks when that happened.
        self._queue_popup_closed_at = 0.0

        self.setWindowTitle(f"{BRAND_NAME} {version_string()}")
        self.setWindowIcon(app_icon())
        # The layout is designed for one exact canvas: a fixed window keeps the
        # player bar, table columns and search rows from ever being squeezed.
        self.setFixedSize(1120, 760)
        # 应用用户保存的主题（默认暖白晨光）。语义色与 QSS 一并切换。
        from ui.theme import apply_theme
        saved_theme = str(load_config().get("theme", current_theme_id()) or current_theme_id())
        apply_theme(saved_theme, self)
        # Any entry point that builds the window gets the shared button cursor,
        # including tests and screenshot scripts that skip main.py.
        app = QApplication.instance()
        if app is not None:
            install_button_cursors(app)
        self._build_ui()
        # The sheet is a sibling top-level window, so it is built after the bar
        # that owns its anchor and before the signals that talk to it.
        self.queue_popup = QueuePopup(self)
        # A press anywhere in the window folds the sheet away. Qt.Popup does
        # this through the window system, which the offscreen test platform and
        # some window managers do not honour; this makes it deterministic.
        QApplication.instance().installEventFilter(self)
        self._wire_signals()
        self._refresh_playlists()
        self._update_queue_badge()
        # Suppress session writes while the initial view is built; the saved
        # session is restored afterwards and must not be overwritten here.
        self._restoring = True
        self._show_default_playlist()
        self._restore_state()
        self._restoring = False
        self._apply_background_from_config(load_config())
        self._sync_bundled_sources()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        # Keep the wallpaper aspect-fill correct when the window changes size.
        layer = getattr(self, "background_layer", None)
        if layer is not None:
            layer.refresh()

    def _apply_background_from_config(self, config: dict[str, Any]) -> None:
        path = str(config.get("background_image", "") or "").strip()
        ok = False
        if path:
            ok = self.background_layer.set_background(path)
        else:
            self.background_layer.clear()
        # 背景状态记录（不再改变组件外观）；透明度统一固定 20%，由背景层自持。
        set_background_active(ok, self)

    def _sync_bundled_sources(self) -> None:
        """Mirror ``sources/`` into the installed list on a background thread.

        Scripts are the only source of truth now, so this runs on every launch:
        new files get probed, edited ones re-probed, unchanged ones skipped.
        Parsing starts a Node sandbox, which must never delay the window.
        """

        def worker() -> None:
            try:
                from core.lx_bootstrap import sync_bundled_sources

                sync_bundled_sources()
            except Exception:  # noqa: BLE001 - best effort, never crash the UI
                pass

        threading.Thread(target=worker, name="lx-sync", daemon=True).start()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        # 背景层置顶：界面先入栈（page 0），BackgroundLayer 后入栈（page 1）。
        # StackAll 让两层同时显示，后入栈的背景层盖在最上面；它按用户设定的
        # 透明度用普通 SourceOver 绘制背景图/视频，并忽略鼠标事件，所以点击
        # 照常落到界面上。透明度只作用于背景层自身，组件面板一律不透明实色。
        from PySide6.QtWidgets import QStackedLayout

        self._window_stack = QStackedLayout(central)
        self._window_stack.setContentsMargins(0, 0, 0, 0)
        self._window_stack.setStackingMode(QStackedLayout.StackingMode.StackAll)

        interface = QWidget()
        root = QHBoxLayout(interface)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        interface_widget = interface
        self._window_stack.addWidget(interface_widget)
        self._window_stack.setCurrentWidget(interface_widget)

        self.background_layer = BackgroundLayer(central)
        self._window_stack.addWidget(self.background_layer)
        self.background_layer.raise_()

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(252)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(14, 0, 14, 14)
        side_layout.setSpacing(0)

        # Brand block: emblem + name + quiet tagline.
        brand = QFrame()
        brand.setObjectName("brandCard")
        brand.setFixedHeight(80)
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(8, 8, 8, 8)
        brand_layout.setSpacing(12)
        emblem = QLabel()
        emblem.setObjectName("brandLogo")
        emblem.setPixmap(logo_pixmap(44))
        emblem.setFixedSize(44, 44)
        emblem.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_layout.addWidget(emblem)
        brand_text = transparent_panel(QWidget())
        brand_text_layout = QVBoxLayout(brand_text)
        brand_text_layout.setContentsMargins(0, 0, 0, 0)
        brand_text_layout.setSpacing(2)
        brand_name = QLabel(BRAND_NAME)
        brand_name.setObjectName("brandName")
        brand_tagline = QLabel(BRAND_SLOGAN)
        brand_tagline.setObjectName("brandTagline")
        brand_text_layout.addWidget(brand_name)
        brand_text_layout.addWidget(brand_tagline)
        brand_layout.addWidget(brand_text, stretch=1)
        side_layout.addWidget(brand)
        side_layout.addSpacing(18)

        # Playlists are the only thing the sidebar lists. Search lives in the
        # topbar: every track has to sit in a playlist, so the sidebar stays
        # purely a playlist index.
        section = QLabel("我的歌单")
        section.setObjectName("sectionLabel")
        side_layout.addWidget(section)

        self.playlist_list = QListWidget()
        self.playlist_list.setObjectName("playlistList")
        self.playlist_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        # A click is the whole interaction here; leaving focus on the row only
        # painted a dotted rectangle around it.
        self.playlist_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        side_layout.addWidget(self.playlist_list, stretch=1)

        self.new_playlist_button = QPushButton("新建歌单")
        self.new_playlist_button.setObjectName("navButton")
        self.new_playlist_button.setIcon(QIcon(plus_pixmap(14, theme_color("ICON"))))
        self.new_playlist_button.setIconSize(QSize(14, 14))
        side_layout.addWidget(self.new_playlist_button)
        side_layout.addSpacing(12)

        self.stats_label = QLabel("")
        self.stats_label.setObjectName("statsCard")
        self.stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side_layout.addWidget(self.stats_label)
        root.addWidget(sidebar)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(16, 12, 16, 12)
        right_layout.setSpacing(10)

        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(68)
        top_layout = QHBoxLayout(topbar)
        top_layout.setContentsMargins(18, 0, 18, 0)
        top_layout.setSpacing(10)

        title_box = transparent_panel(QWidget())
        title_box_layout = QVBoxLayout(title_box)
        title_box_layout.setContentsMargins(0, 0, 0, 0)
        title_box_layout.setSpacing(3)
        self.view_title = QLabel("我的歌单")
        self.view_title.setObjectName("viewTitle")
        self.view_subtitle = QLabel("")
        self.view_subtitle.setObjectName("viewSubtitle")
        title_box_layout.addWidget(self.view_title)
        title_box_layout.addWidget(self.view_subtitle)
        # The title block owns the free space instead of sitting at its hint
        # width: a long subtitle ("… · 正在播放第 12 首") would otherwise be
        # clipped the first time the text grows.
        top_layout.addWidget(title_box, stretch=1)
        # Search lives in the same cluster as the import and settings keys:
        # one field, one key. Pressing either searches every source and the
        # shared content list renders the results.
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("searchInput")
        self.search_edit.setPlaceholderText("搜索歌曲、歌手或专辑，回车开始")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(300)
        self.search_edit.addAction(
            QIcon(magnifier_pixmap(14)), QLineEdit.ActionPosition.LeadingPosition
        )
        self.search_button = QPushButton("搜索")
        self._mark_topbar_button(self.search_button, primary=True)
        top_layout.addWidget(self.search_edit)
        top_layout.addWidget(self.search_button)
        # One key for both import paths: the click folds out a menu with the
        # local-file and the 抖音 share-code entries.
        self.import_button = QPushButton("导入")
        self._mark_topbar_button(self.import_button, primary=False)
        self.import_button.setToolTip("导入本地音乐，或粘贴抖音分享口令")
        self.import_menu = QMenu(self.import_button)
        self.import_menu.addAction("导入本地音乐…", self._import_local_files)
        self.import_menu.addAction("抖音分享口令导入…", self._import_douyin)
        self.import_button.setMenu(self.import_menu)
        self.settings_button = QPushButton("设置")
        self._mark_topbar_button(self.settings_button)
        top_layout.addWidget(self.import_button)
        top_layout.addWidget(self.settings_button)
        right_layout.addWidget(topbar)

        self.table = QTableWidget()
        self.table.setObjectName("trackTable")
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["标题", "歌手", "专辑", "时长", "来源"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(52)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        # The main screen is one list that changes with the topbar and the
        # sidebar: a playlist row renders the playlist table, a search from
        # the topbar renders the aggregated results. A stack keeps exactly one
        # of the two visible at a time.
        self.search_page = SearchPage(
            self,
            player=self.player,
            library=self.library,
            search_edit=self.search_edit,
            search_button=self.search_button,
        )
        self.content_stack = QStackedWidget()
        self.content_stack.setObjectName("contentStack")
        self.content_stack.addWidget(self.table)
        self.content_stack.addWidget(self.search_page)
        right_layout.addWidget(self.content_stack, stretch=1)

        # ---------- Player bar ----------
        # Three columns on one midline: what is playing, how it plays, and the
        # utilities. Every column centres itself vertically instead of relying
        # on the box layout's own distribution, which is what used to leave the
        # transport floating in the upper half of a taller bar.
        player_bar = QFrame()
        player_bar.setObjectName("playerBar")
        player_bar.setFixedHeight(BAR_HEIGHT)
        bar_layout = QHBoxLayout(player_bar)
        bar_layout.setContentsMargins(16, 0, 12, 0)
        bar_layout.setSpacing(16)

        info_widget = transparent_panel(QWidget())
        info_widget.setFixedWidth(INFO_WIDTH)
        info_outer = QVBoxLayout(info_widget)
        info_outer.setContentsMargins(0, 0, 0, 0)
        info_outer.setSpacing(0)
        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 0, 0, 0)
        info_row.setSpacing(12)
        self.cover_label = QLabel()
        self.cover_label.setObjectName("coverLabel")
        self.cover_label.setFixedSize(COVER_SIZE, COVER_SIZE)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setPixmap(note_pixmap(22, theme_color("ACCENT")))
        info_row.addWidget(self.cover_label)
        text_widget = transparent_panel(QWidget())
        text_layout = QVBoxLayout(text_widget)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)
        self.track_title_label = QLabel("未播放")
        self.track_title_label.setObjectName("trackTitle")
        self.track_subtitle_label = QLabel("")
        self.track_subtitle_label.setObjectName("trackSubtitle")
        # The column never grows: long titles elide instead of shoving the
        # transport off the middle of the bar.
        for label in (self.track_title_label, self.track_subtitle_label):
            label.setFixedWidth(INFO_TEXT_WIDTH)
        text_layout.addStretch(1)
        text_layout.addWidget(self.track_title_label)
        text_layout.addWidget(self.track_subtitle_label)
        text_layout.addStretch(1)
        info_row.addWidget(text_widget, stretch=1)
        info_outer.addStretch(1)
        info_outer.addLayout(info_row)
        info_outer.addStretch(1)
        bar_layout.addWidget(info_widget)

        center_widget = transparent_panel(QWidget())
        center_widget.setMinimumWidth(240)
        center_layout = QVBoxLayout(center_widget)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(7)
        center_layout.addStretch(1)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.previous_button = QPushButton()
        self.previous_button.setObjectName("controlButton")
        self.previous_button.setIcon(QIcon(transport_pixmap("prev", 18, theme_color("ICON"))))
        self.previous_button.setIconSize(QSize(18, 18))
        self.play_button = QPushButton()
        self.play_button.setObjectName("playButton")
        self.play_button.setIcon(QIcon(transport_pixmap("play", 24, theme_color("ON_ACCENT"))))
        self.play_button.setIconSize(QSize(24, 24))
        self.next_button = QPushButton()
        self.next_button.setObjectName("controlButton")
        self.next_button.setIcon(QIcon(transport_pixmap("next", 18, theme_color("ICON"))))
        self.next_button.setIconSize(QSize(18, 18))
        # Exactly the trio sits in the middle; the mode key moved to the
        # utilities so the transport is centred over the progress rail below.
        controls.addStretch(1)
        controls.addWidget(self.previous_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.next_button)
        controls.addStretch(1)
        center_layout.addLayout(controls)

        progress = QHBoxLayout()
        progress.setSpacing(10)
        self.elapsed_label = QLabel("00:00")
        self.elapsed_label.setObjectName("timeLabel")
        self.elapsed_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.position_slider = SeekSlider(self)
        self.position_slider.setObjectName("positionSlider")
        self.position_slider.setRange(0, 0)
        self.total_label = QLabel("00:00")
        self.total_label.setObjectName("timeLabel")
        self.total_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        # Equal, fixed time cells keep the rail's centre on the transport's.
        for label in (self.elapsed_label, self.total_label):
            label.setFixedWidth(TIME_WIDTH)
        progress.addWidget(self.elapsed_label)
        progress.addWidget(self.position_slider, stretch=1)
        progress.addWidget(self.total_label)
        center_layout.addLayout(progress)
        center_layout.addStretch(1)
        bar_layout.addWidget(center_widget, stretch=1)

        # Utilities, one quiet row: volume, then the three icon keys.
        utility_widget = transparent_panel(QWidget())
        utility_outer = QVBoxLayout(utility_widget)
        utility_outer.setContentsMargins(0, 0, 0, 0)
        utility_outer.setSpacing(0)
        utility_row = QHBoxLayout()
        utility_row.setContentsMargins(0, 0, 0, 0)
        utility_row.setSpacing(4)

        self.volume_icon = QLabel()
        self.volume_icon.setObjectName("volumeIcon")
        self.volume_icon.setFixedSize(22, 22)
        self.volume_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.volume_icon.setPixmap(volume_pixmap(16, theme_color("FAINT")))
        self.volume_icon.setToolTip("音量")
        utility_row.addWidget(self.volume_icon)
        self.volume_slider = ClickableSlider()
        self.volume_slider.setObjectName("volumeSlider")
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setFixedWidth(VOLUME_WIDTH)
        self.volume_slider.setToolTip("音量")
        utility_row.addWidget(self.volume_slider)
        utility_row.addSpacing(6)

        self.mode_button = QPushButton()
        self.mode_button.setObjectName("utilityButton")
        self.mode_button.setFixedSize(UTILITY_KEY, UTILITY_KEY)
        self.mode_button.setIconSize(QSize(17, 17))
        utility_row.addWidget(self.mode_button)

        self.device_button = QPushButton()
        self.device_button.setObjectName("utilityButton")
        self.device_button.setFixedSize(UTILITY_KEY, UTILITY_KEY)
        self.device_button.setIcon(QIcon(device_pixmap(17, theme_color("ICON"))))
        self.device_button.setIconSize(QSize(17, 17))
        self.device_button.setToolTip("切换音频输出设备（耳机 / 音响 / 扬声器）")
        utility_row.addWidget(self.device_button)

        # The queue key sits in the corner the player bar ends at, and the
        # sheet opens upwards from it — the same gesture as QQ Music. The box
        # is larger than the key so the count can sit on its shoulder.
        self.queue_button_box = QWidget()
        self.queue_button_box.setFixedSize(QUEUE_BOX, QUEUE_BOX)
        self.queue_button = QPushButton(self.queue_button_box)
        self.queue_button.setObjectName("utilityButton")
        self.queue_button.setIcon(QIcon(queue_pixmap(17, theme_color("ICON"))))
        self.queue_button.setIconSize(QSize(17, 17))
        self.queue_button.setGeometry(
            (QUEUE_BOX - UTILITY_KEY) // 2,
            (QUEUE_BOX - UTILITY_KEY) // 2,
            UTILITY_KEY,
            UTILITY_KEY,
        )
        self.queue_button.setToolTip("正在播放列表")
        self.queue_badge = QLabel(self.queue_button_box)
        self.queue_badge.setObjectName("queueBadge")
        self.queue_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # The count is decoration: clicks belong to the key underneath.
        self.queue_badge.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self.queue_badge.hide()
        utility_row.addWidget(self.queue_button_box)
        utility_outer.addStretch(1)
        utility_outer.addLayout(utility_row)
        utility_outer.addStretch(1)
        bar_layout.addWidget(utility_widget)
        right_layout.addWidget(player_bar)

        notice = QLabel(COPYRIGHT_NOTICE)
        notice.setObjectName("copyrightNotice")
        notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        notice.setWordWrap(True)
        notice.setToolTip(COPYRIGHT_NOTICE)
        right_layout.addWidget(notice)

        root.addWidget(right)

    def _wire_signals(self) -> None:
        self.new_playlist_button.clicked.connect(self._create_playlist)
        self.playlist_list.itemClicked.connect(self._on_playlist_clicked)
        self.playlist_list.itemDoubleClicked.connect(self._play_playlist)
        self.playlist_list.customContextMenuRequested.connect(self._show_playlist_menu)
        self.queue_button.clicked.connect(self._toggle_queue_popup)
        self.queue_popup.playRequested.connect(self.player.play_at)
        self.queue_popup.removeRequested.connect(self._remove_queue_tracks)
        self.queue_popup.clearRequested.connect(self._clear_queue)
        self.queue_popup.closed.connect(self._on_queue_popup_closed)
        self.settings_button.clicked.connect(self._open_settings)
        self.table.doubleClicked.connect(self._play_row)
        self.table.customContextMenuRequested.connect(self._show_track_menu)
        self.search_page.imported.connect(self._on_search_imported)
        self.search_page.playlistsChanged.connect(self._refresh_playlists)
        self.search_edit.returnPressed.connect(self._on_topbar_search)
        self.search_button.clicked.connect(self._on_topbar_search)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self._focus_search)

        self.previous_button.clicked.connect(self.player.previous)
        self.play_button.clicked.connect(self.player.toggle_play)
        self.next_button.clicked.connect(self.player.next)
        self.mode_button.clicked.connect(self._cycle_mode)
        self.position_slider.sliderPressed.connect(lambda: setattr(self, "_slider_dragging", True))
        self.position_slider.sliderReleased.connect(self._on_slider_released)
        self.position_slider.seeked.connect(self._on_seek_requested)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        self.device_button.clicked.connect(self._show_device_menu)

        self.player.devicesChanged.connect(self._refresh_device_button)
        self.player.audioDeviceChanged.connect(self._on_audio_device_changed)
        self.player.trackChanged.connect(self._on_track_changed)
        self.player.stateChanged.connect(self._on_state_changed)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.modeChanged.connect(self._on_mode_changed)
        self.player.queueChanged.connect(self._on_queue_changed)
        self.player.errorOccurred.connect(self._on_player_error)

    def _restore_state(self) -> None:
        state = load_playback_state()
        config = load_config()
        with QSignalBlocker(self.volume_slider):
            self.volume_slider.setValue(
                int(float(state.get("volume", config.get("volume", 0.7))) * 100)
            )
        self._on_mode_changed(self.player.mode)
        self._refresh_device_button()
        self._restore_listening_session()

    # ---------- Audio device ----------

    def _refresh_device_button(self) -> None:
        """The key is icon-only now; the current device lives in its tooltip."""
        devices = self.player.audio_devices()
        current_id = self.player.current_device_id()
        if not devices:
            self.device_button.setToolTip("未检测到可用输出设备，将使用系统默认设备")
            return
        name = next(
            (item["name"] for item in devices if item["id"] == current_id),
            "系统默认",
        )
        self.device_button.setToolTip(f"当前输出：{name}\n点击切换设备")

    def _on_audio_device_changed(self, device_id: str) -> None:
        self._refresh_device_button()

    def _show_device_menu(self) -> None:
        menu = QMenu(self)
        devices = self.player.audio_devices()
        current_id = self.player.current_device_id()

        default_action = menu.addAction("系统默认设备")
        default_action.setCheckable(True)
        default_action.setChecked(not current_id)
        default_action.triggered.connect(lambda: self._select_device(""))
        if devices:
            menu.addSeparator()
        for device in devices:
            action = menu.addAction(device["name"])
            action.setCheckable(True)
            action.setChecked(device["id"] == current_id)
            action.triggered.connect(
                lambda checked=False, did=device["id"]: self._select_device(did)
            )
        if not devices:
            note = menu.addAction("当前环境未检测到音频输出设备")
            note.setEnabled(False)
        # The bar sits at the bottom edge, so the list folds upwards from the
        # key and keeps its right edge on it.
        hint = menu.sizeHint()
        menu.exec(
            self.device_button.mapToGlobal(
                QPoint(self.device_button.width() - hint.width(), -hint.height() - 6)
            )
        )

    def _select_device(self, device_id: str) -> None:
        if not self.player.set_audio_device(device_id):
            warning(self, "设备切换失败", "所选设备不可用或已被拔出，请重新选择")

    # ---------- Listening session ----------

    def _persist_session(self, position_ms: int | None = None) -> None:
        self.player.save_session(self._view_playlist_id, position_ms)

    def _restore_listening_session(self) -> None:
        self._restoring = True
        try:
            self._restore_listening_session_inner()
        finally:
            self._restoring = False

    def _restore_listening_session_inner(self) -> None:
        state = load_playback_state()
        # The last playlist is restored even when nothing was playing: it is
        # the only view there is now, so a cold start should not jump to the
        # default bucket for someone who lives in another list.
        saved_view = str(state.get("playlist_id", ""))
        if saved_view in self.library.playlists:
            self._show_playlist(saved_view)
        track_id = state.get("track_id", "")
        if not track_id or track_id not in self.library.tracks:
            return
        # The saved list may be gone (deleted playlist) or may have lost the
        # track; fall back to whichever playlist still holds it, then to the
        # default bucket. There is no whole-library view to fall back on.
        playlist_id = saved_view
        playlist = self.library.playlists.get(playlist_id)
        if playlist is None or track_id not in playlist.track_ids:
            holders = self.library.playlists_containing(track_id)
            playlist_id = holders[0] if holders else self.library.default_playlist_id()
        candidates = self.library.tracks_for_playlist(playlist_id)
        index = next((i for i, track in enumerate(candidates) if track.id == track_id), -1)
        self._show_playlist(playlist_id)
        if index < 0:
            return
        self.player.restore_session(candidates, index, int(state.get("position_ms", 0)))
        self.table.selectRow(index)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self, db_relocator=self._relocate_database)
        dialog.settingsSaved.connect(self._apply_settings)
        # 主题/背景点击后即时预览，不等"保存设置"。
        # 透明度已统一固定为 20%，不再提供预览。
        dialog.themePreview.connect(self._preview_theme)
        dialog.backgroundPreview.connect(self._preview_background)
        # 记录打开前的状态，取消时整体回滚，避免"预览了但没保存"造成错觉。
        self._theme_on_dialog_open = current_theme_id()
        background_before = self.background_layer.background_path()

        def restore_on_reject() -> None:
            from ui.theme import apply_theme
            apply_theme(self._theme_on_dialog_open, self)
            self._refresh_icons_for_theme()
            if background_before:
                self.background_layer.set_background(background_before)
            else:
                self.background_layer.clear()
            set_background_active(bool(background_before), self)

        dialog.rejected.connect(restore_on_reject)
        dialog.exec()

    def _preview_theme(self, theme_id: str) -> None:
        """设置里点主题按钮 → 立即应用到整个窗口。"""
        from ui.theme import apply_theme
        apply_theme(theme_id, self)
        self._refresh_icons_for_theme()

    def _preview_background(self, path: str) -> None:
        """设置里选择/清空背景 → 立即预览，尚未写入配置。"""
        ok = False
        if path:
            ok = self.background_layer.set_background(path)
        else:
            self.background_layer.clear()
        set_background_active(ok, self)

    def _refresh_icons_for_theme(self) -> None:
        """重设所有烘焙进 QPixmap 的图标，使其跟随当前主题色。"""
        # 主题切换会重绘背景层（它用主题地面色做底衬）。
        self.background_layer.update()
        icon = theme_color("ICON")
        accent = theme_color("ACCENT")
        on_accent = theme_color("ON_ACCENT")
        faint = theme_color("FAINT")
        self.new_playlist_button.setIcon(QIcon(plus_pixmap(14, icon)))
        self.cover_label.setPixmap(note_pixmap(22, accent))
        self.previous_button.setIcon(QIcon(transport_pixmap("prev", 18, icon)))
        self.play_button.setIcon(
            QIcon(transport_pixmap("pause" if self.player.is_playing else "play", 24, on_accent))
        )
        self.next_button.setIcon(QIcon(transport_pixmap("next", 18, icon)))
        self.volume_icon.setPixmap(volume_pixmap(16, faint))
        self.device_button.setIcon(QIcon(device_pixmap(17, icon)))
        self.queue_button.setIcon(QIcon(queue_pixmap(17, icon)))
        self._on_mode_changed(self.player.mode)
        # 当前曲目行/列表高亮也随主题变化，重建视图即可。
        self._refresh_table()
        current = self.player.current_track
        current_id = current.id if current is not None else ""
        if hasattr(self, "queue_view") and hasattr(self.queue_view, "set_tracks"):
            self.queue_view.set_tracks(self.queue_view._tracks, current_id)

    def _relocate_database(self, target: Path) -> str | None:
        """Move the database, then rebind every object holding a connection."""

        def close_library() -> None:
            self.library.close()

        error = switch_database_file(target, before_switch=close_library)
        # Rebuild even on failure: the helper may have closed both handles
        # before an error, and the UI must never keep a dead Library. Close
        # explicitly as well - the early "target exists" guard never reaches
        # before_switch, and leaking that handle would lock the old file on
        # Windows.
        try:
            self.library.close()
        except Exception:  # noqa: BLE001 - already closed by the relocator
            pass
        try:
            self.library = Library()
            self.search_page.library = self.library
            self._refresh_playlists()
            if self._view_playlist_id in self.library.playlists:
                self._show_playlist(self._view_playlist_id)
            else:
                self._show_default_playlist()
        except Exception as exc:  # noqa: BLE001 - surfaced to the settings dialog
            return f"数据库已切换，但界面刷新失败：{exc}"
        return error

    def _apply_settings(self, config: dict[str, Any]) -> None:
        # 主题在点击时已预览应用；保存后以持久化的值为准再确保一次。
        from ui.theme import apply_theme
        saved_theme = str(config.get("theme", current_theme_id()) or current_theme_id())
        if saved_theme != current_theme_id():
            apply_theme(saved_theme, self)
            self._refresh_icons_for_theme()
        try:
            mode = PlaybackMode(str(config.get("playback_mode", "list_loop")))
        except ValueError:
            mode = PlaybackMode.LIST_LOOP
        if self.player.mode != mode:
            self.player.set_mode(mode)
        self._on_mode_changed(mode)
        volume = float(config.get("volume", 0.7))
        with QSignalBlocker(self.volume_slider):
            self.volume_slider.setValue(int(volume * 100))
        self.player.set_volume(volume)
        self._apply_background_from_config(config)
        show_info(
            self,
            "设置已保存",
            "偏好已写入本地数据库，下次启动会自动恢复。\n"
            f"数据库位置：{current_db_file()}\n"
            f"抖音缓存目录：{config.get('cache_dir', '')}\n"
            f"聚合搜索：超时 {config.get('search_timeout_seconds', 15)} 秒，"
            f"每个音源最多 {config.get('search_limit_per_source', 30)} 首",
        )

    # ---------- Library / view ----------

    def _playlist_row_text(self, name: str, count: int) -> str:
        """Name left, count right, in a column that lines up across rows.

        A plain "name   count" string puts every count at a different x; the
        name is padded to a fixed field so the numbers stack instead.
        """
        metrics = QFontMetrics(self.playlist_list.font())
        field = 120
        label = metrics.elidedText(name, Qt.TextElideMode.ElideRight, field)
        padding = ""
        while field - metrics.horizontalAdvance(label + padding) > 4:
            padding += " "
        return f"▤  {label}{padding}{count}"

    def _refresh_playlists(self) -> None:
        self.playlist_list.clear()
        default_id = self.library.default_playlist_id()
        for playlist in self.library.ordered_playlists():
            count = len(self.library.tracks_for_playlist(playlist.id))
            item = QListWidgetItem(self._playlist_row_text(playlist.name, count))
            item.setData(Qt.ItemDataRole.UserRole, playlist.id)
            if playlist.id == default_id:
                item.setToolTip(
                    f"默认歌单 · {count} 首\n导入时未指定歌单的音乐会加入这里"
                )
            else:
                item.setToolTip(f"{playlist.name} · {count} 首")
            self.playlist_list.addItem(item)
        # While search results are on screen no playlist is highlighted: the
        # sidebar then describes nothing the user is looking at.
        if self._in_search_view():
            self.playlist_list.setCurrentRow(-1)
            self.playlist_list.clearSelection()
        else:
            self._select_playlist_row(self._view_playlist_id)
        self._refresh_stats()

    def _in_search_view(self) -> bool:
        return self.content_stack.currentWidget() is self.search_page

    def _select_playlist_row(self, playlist_id: str) -> None:
        for row in range(self.playlist_list.count()):
            item = self.playlist_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == playlist_id:
                self.playlist_list.setCurrentItem(item)
                return
        self.playlist_list.clearSelection()

    def _refresh_stats(self) -> None:
        self.stats_label.setText(
            f"{len(self.library.tracks)} 首曲目   ·   {len(self.library.playlists)} 个歌单"
        )

    def _current_playlist_id(self) -> str:
        """The playlist on screen; the default bucket whenever it is unknown."""
        if self._view_playlist_id in self.library.playlists:
            return self._view_playlist_id
        return self.library.default_playlist_id()

    def _refresh_table(self) -> None:
        tracks = self.library.tracks_for_playlist(self._current_playlist_id())
        self._displayed_tracks = tracks
        self.table.setRowCount(len(tracks))
        current = self.player.current_track
        for row, track in enumerate(tracks):
            is_current = current is not None and current.id == track.id
            title_item = QTableWidgetItem(("▸  " if is_current else "   ") + track.title)
            title_item.setData(Qt.ItemDataRole.UserRole, track.id)
            if track.cover_file:
                pixmap = QPixmap(str(COVER_DIR / track.cover_file))
                if not pixmap.isNull():
                    title_item.setIcon(pixmap.scaled(34, 34, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            if is_current:
                title_item.setForeground(QColor(theme_color("ACCENT_DEEP")))
                font = title_item.font()
                font.setBold(True)
                title_item.setFont(font)
            self.table.setItem(row, 0, title_item)
            self.table.setItem(row, 1, QTableWidgetItem(track.artist))
            self.table.setItem(row, 2, QTableWidgetItem(track.album))
            self.table.setItem(row, 3, QTableWidgetItem(format_duration(track.duration_ms)))
            # Never name the platform a track was fetched from; the aggregate
            # label keeps the column honest without pointing at anyone.
            if track.source == "douyin":
                source_label = "抖音"
            elif track.source.startswith("search:"):
                source_label = "在线"
            else:
                source_label = "本地"
            self.table.setItem(row, 4, QTableWidgetItem(source_label))
            if is_current:
                for column in range(self.table.columnCount()):
                    self.table.item(row, column).setBackground(QColor(theme_color("TINT")))
        self._refresh_stats()
        self._refresh_view_heading()

    def _refresh_view_heading(self) -> None:
        """Keep the topbar in step with whatever the list is showing."""
        if self._in_search_view():
            self.view_title.setText("聚合搜索")
            self.view_subtitle.setText("输入关键词，同时检索全部可用音源")
            return
        playlist = self.library.playlists.get(self._current_playlist_id())
        if playlist is None:
            return
        count = len(self.library.tracks_for_playlist(playlist.id))
        self.view_title.setText(playlist.name)
        if count:
            self.view_subtitle.setText(f"歌单 · {count} 首曲目")
        else:
            self.view_subtitle.setText(
                "歌单 · 0 首曲目 · 右上角导入或在线搜索"
            )

    def _show_default_playlist(self) -> None:
        self._show_playlist(self.library.default_playlist_id())

    def _focus_search(self) -> None:
        """Ctrl+F drops the caret in the topbar search field."""
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    @staticmethod
    def _mark_topbar_button(button: QPushButton, *, primary: bool = False) -> None:
        """Every control in the topbar row shares one geometry spec.

        Height, radius and font come from #topbarButton in the theme; the
        variant property only picks the colour (filled blue vs. outline).
        """
        button.setObjectName("topbarButton")
        button.setProperty("variant", "primary" if primary else "plain")

    def _toggle_queue_popup(self) -> None:
        """Fold the queue out of the transport, QQ-Music style.

        A sheet rather than a destination: the queue belongs to the player
        bar, so it opens right above the button that owns it and closes when
        the user clicks anywhere else.
        """
        if self.queue_popup.isVisible():
            self.queue_popup.hide()
            return
        # A press on the key while the sheet is open closes it first, and the
        # resulting click would reopen it immediately. Ignore that echo.
        if time.monotonic() - self._queue_popup_closed_at < 0.25:
            return
        self._refresh_queue_popup()
        self.queue_popup.open_at(self.queue_button_box)

    def _on_queue_popup_closed(self) -> None:
        self._queue_popup_closed_at = time.monotonic()

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802
        """Close the queue sheet on any press outside of it."""
        if (
            self.queue_popup.isVisible()
            and event.type() == QEvent.Type.MouseButtonPress
            and isinstance(watched, QWidget)
            and not self.queue_popup.isAncestorOf(watched)
        ):
            self.queue_popup.hide()
        return super().eventFilter(watched, event)

    def _refresh_queue_popup(self) -> None:
        """Redraw the sheet; an audition is not part of the real queue."""
        current = self.player.current_track
        current_id = (
            ""
            if self.player.is_previewing or current is None
            else current.id
        )
        self.queue_popup.set_queue(self.player.queue, current_id)

    def _update_queue_badge(self) -> None:
        """A small count on the key: how many songs wait in the queue."""
        count = len(self.player.queue)
        if not count:
            self.queue_badge.hide()
            return
        text = str(count) if count < 100 else "99+"
        width = 17 + 5 * (len(text) - 1)
        self.queue_badge.setText(text)
        self.queue_badge.setFixedSize(width, 16)
        self.queue_badge.move(self.queue_button_box.width() - width - 1, 0)
        self.queue_badge.show()

    def _on_queue_changed(self) -> None:
        """Keep the queue key's count and the open sheet in step."""
        self._update_queue_badge()
        if self.queue_popup.isVisible():
            self._refresh_queue_popup()

    def _remove_queue_tracks(self, track_ids: list[str]) -> None:
        """Drop rows from the queue only — files and playlists stay untouched."""
        if not track_ids:
            return
        self.player.remove_from_queue(track_ids)

    def _clear_queue(self) -> None:
        if not self.player.queue:
            return
        answer = ask_confirm(
            self,
            "清空播放列表",
            "清空正在播放列表并停止播放？媒体库和歌单中的音乐会保留。",
        )
        if not answer:
            return
        self.player.clear_queue()

    def _show_playlist(self, playlist_id: str) -> None:
        playlist = self.library.playlists.get(playlist_id)
        if playlist is None:
            playlist = self.library.ensure_default_playlist()
        # Leaving the search view hands the transport back to the library:
        # an audition belongs to no playlist, so it must not survive the swap.
        self.search_page.set_active(False)
        self._view_playlist_id = playlist.id
        self.content_stack.setCurrentWidget(self.table)
        self._select_playlist_row(playlist.id)
        self._refresh_table()
        if not self._restoring:
            self._persist_session()

    def _show_search_view(self) -> None:
        """Swap the one list on the main screen over to aggregated search."""
        self.content_stack.setCurrentWidget(self.search_page)
        self.playlist_list.setCurrentRow(-1)
        self.playlist_list.clearSelection()
        self._refresh_view_heading()
        self.search_page.set_active(True)

    def _on_topbar_search(self) -> None:
        """The topbar key (or Enter in the field) searches and swaps the list."""
        if not self.search_edit.text().strip():
            self.search_edit.setFocus()
            return
        self._show_search_view()
        self.search_page._on_search()

    def _on_playlist_clicked(self, item: QListWidgetItem) -> None:
        """One click opens a playlist; double click also starts playing it.

        With the sidebar as the only navigation, selection alone cannot be a
        no-op — and double-clicking used to be the only way to see a list.
        """
        self._show_playlist(item.data(Qt.ItemDataRole.UserRole))

    def _create_playlist(self) -> None:
        dialog = NewPlaylistDialog(self)
        if dialog.exec() != NewPlaylistDialog.DialogCode.Accepted:
            return
        playlist = self.library.create_playlist(dialog.name)
        self._refresh_playlists()
        self._show_playlist(playlist.id)

    def _play_playlist(self, item: QListWidgetItem) -> None:
        playlist_id = item.data(Qt.ItemDataRole.UserRole)
        self._show_playlist(playlist_id)
        tracks = self.library.tracks_for_playlist(playlist_id)
        if not tracks:
            show_info(self, "空歌单", "该歌单还没有音乐，请先添加曲目")
            return
        self.player.play_tracks(tracks, 0)

    def _play_row(self, index) -> None:
        if 0 <= index.row() < len(self._displayed_tracks):
            self.player.play_tracks(self._displayed_tracks, index.row())

    def _selected_track_ids(self) -> list[str]:
        result = []
        for model_index in self.table.selectionModel().selectedRows():
            item = self.table.item(model_index.row(), 0)
            if item is not None:
                result.append(item.data(Qt.ItemDataRole.UserRole))
        return result

    # ---------- Import ----------

    def _on_search_imported(self, count: int) -> None:
        # A download landed in the library: redraw whatever the sidebar is
        # showing so counts stay honest, without yanking the user off the
        # search they are browsing.
        self._refresh_playlists()
        self._refresh_table()

    def _ask_target_playlist(self, title: str, hint: str = "") -> str:
        """Ask where an import lands; empty when the user backs out.

        Every import names a playlist, because playlists are the only view of
        the library. The default bucket is preselected so "just import it"
        stays a single key press.
        """
        default_id = self.library.default_playlist_id()
        options = [
            (
                playlist.id,
                f"{playlist.name}（{len(self.library.tracks_for_playlist(playlist.id))} 首）",
            )
            for playlist in self.library.ordered_playlists()
        ]
        dialog = PlaylistPickerDialog(
            self,
            options=options,
            default_id=default_id,
            title=title,
            hint=hint
            or "导入的音乐会加入所选歌单；不选就放进「默认歌单」。",
        )
        if dialog.exec() != PlaylistPickerDialog.DialogCode.Accepted:
            return ""
        return dialog.playlist_id or default_id

    def _import_local_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择音乐文件", "", AUDIO_FILTER)
        if not paths:
            return
        playlist_id = self._ask_target_playlist("导入本地音乐")
        if not playlist_id:
            return
        added, skipped = self.library.add_tracks(paths)
        count = self.library.add_to_playlist(
            playlist_id, [track.id for track in added]
        )
        playlist = self.library.playlists.get(playlist_id)
        name = playlist.name if playlist is not None else "歌单"
        self._refresh_playlists()
        self._show_playlist(playlist_id)
        show_info(
            self,
            "导入完成",
            f"已将 {count} 首加入歌单「{name}」；跳过重复或无效文件 {skipped} 首",
        )

    def _import_douyin(self) -> None:
        # 歌单选择与口令填写合并到同一个对话框，不再先弹一个选择歌单的窗口。
        default_id = self.library.default_playlist_id()
        options = [
            (playlist.id, f"{playlist.name}（{len(self.library.tracks_for_playlist(playlist.id))} 首）")
            for playlist in self.library.ordered_playlists()
        ]
        dialog = DouyinImportDialog(
            self, playlist_options=options, default_playlist_id=default_id
        )
        dialog.imported.connect(
            lambda result: self._on_douyin_imported(result, dialog.playlist_id)
        )
        dialog.exec()

    def _on_douyin_imported(
        self, result: DouyinImportResult, playlist_id: str = ""
    ) -> None:
        track = build_track_from_file(
            result.audio_path,
            source="douyin",
            source_url=result.page_url,
            cover_bytes=result.cover_bytes,
            cover_extension=result.cover_extension,
        )
        if not self.library.add_track(track):
            show_info(self, "重复导入", "该音频已存在于媒体库中")
            return
        target = self.library.playlists.get(playlist_id) if playlist_id else None
        if target is None:
            target = self.library.ensure_default_playlist()
        self.library.add_to_playlist(target.id, [track.id])
        self._refresh_playlists()
        self._show_playlist(target.id)
        show_info(self, "导入完成", f"已将《{track.title}》加入歌单「{target.name}」")

    # ---------- Context menus ----------

    def _show_playlist_menu(self, position) -> None:
        item = self.playlist_list.itemAt(position)
        if item is None:
            return
        playlist_id = item.data(Qt.ItemDataRole.UserRole)
        playlist = self.library.playlists.get(playlist_id)
        if playlist is None:
            return
        menu = QMenu(self)
        if playlist_id == self.library.default_playlist_id():
            # The bucket is addressed by name in every import hint, so it stays
            # put; the menu explains that instead of hiding the row's menu.
            note = menu.addAction("「默认歌单」不可重命名或删除")
            note.setEnabled(False)
            menu.exec(self.playlist_list.mapToGlobal(position))
            return
        rename_action = menu.addAction("重命名")
        delete_action = menu.addAction("删除歌单")
        chosen = menu.exec(self.playlist_list.mapToGlobal(position))
        if chosen is rename_action:
            value = ask_rename(self, playlist.name)
            if value and self.library.rename_playlist(playlist_id, value):
                self._refresh_playlists()
                self._show_playlist(playlist_id)
        elif chosen is delete_action:
            tracks = self.library.tracks_for_playlist(playlist_id)
            count = len(tracks)
            # Songs only this playlist holds are wiped from the library along
            # with it; songs shared with other playlists survive there.
            exclusive_ids = [
                track.id
                for track in tracks
                if len(self.library.playlists_containing(track.id)) == 1
            ]
            current_track = self.player.current_track
            stops_playback = (
                current_track is not None and current_track.id in exclusive_ids
            )
            stop_notice = ""
            if stops_playback and current_track is not None:
                stop_notice = f"\n《{current_track.title}》是当前曲目，删除前会先停止播放。\n"
            answer = ask_danger(
                self,
                "删除歌单",
                f"确定删除歌单「{playlist.name}」？\n"
                f"其中的 {count} 首音乐会同时从媒体库数据库中移除"
                "（音频文件保留在磁盘），"
                "与其他歌单共享的音乐只从本歌单移除。\n"
                f"{stop_notice}",
                confirm_text="删除歌单",
            )
            if answer:
                if stops_playback:
                    self.player.release_source()
                self.player.remove_from_queue(
                    exclusive_ids, already_stopped=stops_playback
                )
                self.library.delete_playlist(playlist_id)
                self._refresh_playlists()
                if self._view_playlist_id == playlist_id:
                    self._show_default_playlist()
                else:
                    self._refresh_table()

    def _show_track_menu(self, position) -> None:
        index = self.table.indexAt(position)
        if not index.isValid():
            return
        self.table.setCurrentCell(index.row(), 0)
        track_ids = self._selected_track_ids()
        if not track_ids:
            return
        menu = QMenu(self)
        menu.addAction("播放", partial(self._play_row, index))
        add_menu = menu.addMenu("添加到歌单")
        for playlist in self.library.ordered_playlists():
            add_menu.addAction(
                playlist.name, partial(self._add_to_playlist, playlist.id, track_ids)
            )
        add_menu.addSeparator()
        add_menu.addAction("新建歌单并添加…", partial(self._new_playlist_with_tracks, track_ids))
        menu.addAction("从当前歌单移除", partial(self._remove_from_current_playlist, track_ids))
        menu.addAction("从媒体库移除（保留文件）", partial(self._remove_from_library, track_ids))
        menu.addAction("删除文件（不可恢复）", partial(self._delete_track_files, track_ids))
        menu.exec(self.table.mapToGlobal(position))

    def _add_to_playlist(self, playlist_id: str, track_ids: list[str]) -> None:
        count = self.library.add_to_playlist(playlist_id, track_ids)
        self._refresh_playlists()
        if self._view_playlist_id == playlist_id:
            self._refresh_table()
        playlist = self.library.playlists.get(playlist_id)
        name = playlist.name if playlist is not None else "歌单"
        if count:
            show_info(self, "已添加", f"向歌单「{name}」添加了 {count} 首音乐")
        else:
            show_info(self, "无需添加", f"选中的音乐已经在歌单「{name}」里了")

    def _new_playlist_with_tracks(self, track_ids: list[str]) -> None:
        dialog = NewPlaylistDialog(self)
        if dialog.exec() != NewPlaylistDialog.DialogCode.Accepted:
            return
        playlist = self.library.create_playlist(dialog.name)
        self.library.add_to_playlist(playlist.id, track_ids)
        self._refresh_playlists()
        self._show_playlist(playlist.id)

    def _remove_from_current_playlist(self, track_ids: list[str]) -> None:
        """Take tracks out of the list on screen.

        A playlist is the only way to see a track now, so the last membership
        is the library entry: removing it there has to say so out loud and
        drop the row from the library (the audio file stays on disk).
        """
        playlist_id = self._current_playlist_id()
        leaving_library = [
            track_id
            for track_id in track_ids
            if self.library.playlists_containing(track_id) == [playlist_id]
        ]
        if leaving_library:
            if not ask_confirm(
                self,
                "移出媒体库",
                f"选中的音乐中有 {len(leaving_library)} 首只属于当前歌单。"
                "移出后将不再出现在媒体库中（音频文件仍保留在磁盘）。确定继续？",
                confirm_text="移出",
            ):
                return
        for track_id in track_ids:
            self.library.remove_from_playlist(playlist_id, track_id)
            if track_id in leaving_library:
                self.library.remove_track(track_id)
        # A row that no longer exists cannot keep playing from the queue.
        self.player.remove_from_queue(leaving_library)
        self._refresh_playlists()
        self._show_playlist(playlist_id)

    def _remove_from_library(self, track_ids: list[str]) -> None:
        answer = ask_confirm(
            self,
            "从媒体库移除",
            "仅从媒体库和歌单中移除，不删除磁盘上的音频文件。确定继续？",
        )
        if not answer:
            return
        self.player.remove_from_queue(track_ids)
        for track_id in track_ids:
            self.library.remove_track(track_id)
        self._refresh_playlists()
        self._show_playlist(self._current_playlist_id())

    def _delete_track_files(self, track_ids: list[str]) -> None:
        tracks = [self.library.tracks[track_id] for track_id in track_ids if track_id in self.library.tracks]
        if not tracks:
            return
        names = "\n".join(f"• {track.title}" for track in tracks[:8])
        if len(tracks) > 8:
            names += f"\n… 以及另外 {len(tracks) - 8} 首"
        current_track = self.player.current_track
        stops_playback = current_track is not None and current_track.id in track_ids
        stop_notice = ""
        if stops_playback and current_track is not None:
            stop_notice = f"\n《{current_track.title}》是当前曲目，删除前会先停止播放。\n"
        answer = ask_danger(
            self,
            "删除文件",
            "此操作将永久删除磁盘上的音频文件，无法恢复（不会进入回收站）：\n\n"
            f"{names}\n\n"
            f"{stop_notice}"
            "曲目将同时从媒体库和所有歌单中移除。确定继续？",
            confirm_text="永久删除",
        )
        if not answer:
            return
        # Only stop when the deleted set contains the currently loaded track.
        # Deleting other rows must not interrupt listening.
        if stops_playback:
            self.player.release_source()
        self.player.remove_from_queue(track_ids, already_stopped=stops_playback)
        deleted, failures = self.library.delete_tracks_with_files(track_ids)
        self._refresh_playlists()
        self._show_playlist(self._current_playlist_id())
        if failures:
            warning(
                self,
                "部分文件未删除",
                f"已从媒体库和歌单移除 {deleted} 首，但以下磁盘文件未能删除，请手动清理：\n\n"
                + "\n".join(failures),
            )
        else:
            show_info(self, "删除完成", f"已永久删除 {deleted} 首音乐及其文件")

    # ---------- Playback UI ----------

    def _cycle_mode(self) -> None:
        current_index = MODE_ORDER.index(self.player.mode)
        self.player.set_mode(MODE_ORDER[(current_index + 1) % len(MODE_ORDER)])

    def _on_mode_changed(self, mode: PlaybackMode) -> None:
        self.mode_button.setIcon(QIcon(mode_pixmap(MODE_ICONS[mode], 17, theme_color("ICON"))))
        self.mode_button.setToolTip(
            f"{MODE_LABELS[mode]}\n{MODE_TOOLTIPS[mode]}\n点击切换播放模式"
        )

    def _on_state_changed(self, playing: bool) -> None:
        self.play_button.setIcon(
            QIcon(transport_pixmap("pause" if playing else "play", 24, theme_color("ON_ACCENT")))
        )
        if not playing and not self._restoring:
            self._persist_session()

    def _on_player_error(self, message: str) -> None:
        """Report a playback error where the user is looking.

        An audition that fails is a search problem, not a library problem: the
        page explains it inline and the transport goes back to the queue
        instead of popping a modal over a song that was never imported.
        """
        if self.player.is_previewing:
            self.search_page.search_hint(f"试听失败：{message}")
            self.player.exit_preview()
            return
        warning(self, "播放提示", message)

    def _set_cover(self, track: Track | None) -> None:
        """Show the art clipped to the frame; the note stands in for none."""
        source = (
            QPixmap(str(COVER_DIR / track.cover_file))
            if track is not None and track.cover_file
            else QPixmap()
        )
        if source.isNull():
            self.cover_label.setPixmap(note_pixmap(22, theme_color("ACCENT")))
            return
        self.cover_label.setPixmap(rounded_pixmap(source, COVER_SIZE, 10))

    def _set_transport_text(self, title: str, subtitle: str) -> None:
        """Two lines, one column: elide so neither can push the bar around."""
        for label, text in (
            (self.track_title_label, title),
            (self.track_subtitle_label, subtitle),
        ):
            metrics = QFontMetrics(label.font())
            label.setText(
                metrics.elidedText(
                    text, Qt.TextElideMode.ElideRight, INFO_TEXT_WIDTH - 2
                )
            )
            label.setToolTip(text)

    def _on_track_changed(self, track: Track | None) -> None:
        if track is None:
            self._set_cover(None)
            self._set_transport_text("未播放", "")
            # The "▸" marker lives in the tables, so emptying the transport has
            # to repaint them as well.
            self._refresh_table()
            if self.queue_popup.isVisible():
                self._refresh_queue_popup()
            return
        self._set_cover(track)
        subtitle = track.artist
        if track.album:
            subtitle = f"{subtitle} · {track.album}"
        if self.player.is_previewing:
            # One transport, one queue: an audition is marked in place instead
            # of getting its own controls. It is not a library track, so the
            # table is neither refreshed nor persisted.
            self._set_transport_text(
                track.title, f"{subtitle} · 试听" if subtitle else "试听"
            )
            return
        self._set_transport_text(track.title, subtitle)
        self._refresh_table()
        if self.queue_popup.isVisible():
            self._refresh_queue_popup()
        if not self._restoring:
            self._persist_session(0)

    def _on_position_changed(self, milliseconds: int) -> None:
        if not self._slider_dragging:
            self.position_slider.setValue(milliseconds)
        self.elapsed_label.setText(format_duration(milliseconds))

    def _on_duration_changed(self, milliseconds: int) -> None:
        self.position_slider.setRange(0, milliseconds)
        self.total_label.setText(format_duration(milliseconds))

    def _on_slider_released(self) -> None:
        self._slider_dragging = False
        self.player.set_position(self.position_slider.value())
        self._persist_session(self.position_slider.value())

    def _on_seek_requested(self, milliseconds: int) -> None:
        """Seek to an exact point after a click (or click-and-scrub)."""
        if self.position_slider.maximum() <= self.position_slider.minimum():
            return
        self._slider_dragging = False
        self.player.set_position(milliseconds)
        self.elapsed_label.setText(format_duration(milliseconds))
        self._persist_session(milliseconds)

    def _on_volume_changed(self, value: int) -> None:
        self.player.set_volume(value / 100.0)
        config = load_config()
        config["volume"] = value / 100.0
        save_config(config)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming convention
        # An app-wide filter outliving the window would be a crash on exit.
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        try:
            self.search_page.shutdown()
        except Exception:  # noqa: BLE001 - never block closing
            pass
        try:
            self._persist_session()
        except Exception:  # noqa: BLE001 - a locked library must not block exit
            pass
        # release_source, not stop: stopping leaves the file loaded and Windows
        # keeps a handle on it, which blocks deletes and temp cleanup.
        self.player.release_source()
        self.library.close()
        super().closeEvent(event)
