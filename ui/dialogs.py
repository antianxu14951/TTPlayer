from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    QSize,
    QThread,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config.settings import (
    CACHE_DIR,
    DATA_DIR,
    current_db_file,
    get_cache_dir,
    load_config,
    save_config,
)
from core.douyin import DouyinImportResult, StepError, import_share
from ui.branding import COPYRIGHT_NOTICE
from ui.texts import localize_buttons, warning


def _swatch_icon(colors: tuple[str, str, str]) -> QIcon:
    """三个实心圆点 = 主色 / 辅色 / 点缀色，让主题差异一眼可见。"""
    pixmap = QPixmap(30, 10)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    for index, color in enumerate(colors):
        painter.setBrush(QColor(color))
        painter.drawEllipse(index * 10 + 1, 1, 8, 8)
    painter.end()
    return QIcon(pixmap)
from ui.theme import current_theme_id, transparent_panel
from ui.themes import THEMES

# Overall progress milestones for the five pipeline stages. Download carries the
# largest weight because it dominates perceived import time; the MP3 extraction
# that follows is folded into the same "导入音频" stage.
STEP_PREFIXES = ("步骤1", "步骤2", "步骤3", "步骤4", "步骤5")
STEP_TITLES = (
    "提取分享链接",
    "解析视频页面",
    "获取作品数据",
    "获取视频地址",
    "导入音频",
)
STEP_TARGETS = (6, 18, 30, 38, 100)
DOWNLOAD_START, DOWNLOAD_END = 38, 88

_DOWNLOAD_PERCENT = re.compile(r"已下载\s*(\d+)\s*%")

_STATE_ICONS = {"pending": "○", "running": "◐", "done": "✓", "error": "✗"}
_STATE_COLORS = {
    "pending": "#939AA9",
    "running": "#F5513A",
    "done": "#1E2333",
    "error": "#C23824",
}


def compute_progress(step_name: str, detail: str) -> int:
    """Map a worker signal to an overall 0-100 percentage."""
    if step_name.startswith("步骤5"):
        match = _DOWNLOAD_PERCENT.search(detail)
        if match:
            percent = min(100, max(0, int(match.group(1))))
            return DOWNLOAD_START + (DOWNLOAD_END - DOWNLOAD_START) * percent // 100
        if "次尝试" in detail:
            return DOWNLOAD_START
        if "正在提取音轨" in detail:
            # 下载完成、ffmpeg 尚未结束时停在下载段末端。
            return DOWNLOAD_END
    for prefix, target in zip(STEP_PREFIXES, STEP_TARGETS):
        if step_name.startswith(prefix):
            return target
    return 0


class DouyinWorker(QObject):
    step = Signal(str, str, bool)
    finished = Signal(object, object)

    def __init__(self, share_text: str) -> None:
        super().__init__()
        self._share_text = share_text

    def run(self) -> None:
        try:
            result = import_share(
                self._share_text,
                lambda name, detail: self.step.emit(name, detail, True),
            )
            self.finished.emit(result, "")
        except StepError as exc:
            self.step.emit(exc.step, exc.message, False)
            self.finished.emit(None, exc.message)
        except Exception as exc:  # pragma: no cover - defensive UI boundary
            self.step.emit("导入失败", str(exc), False)
            self.finished.emit(None, str(exc))


class DouyinImportDialog(QDialog):
    imported = Signal(object)

    def __init__(
        self,
        parent=None,
        playlist_options: list[tuple[str, str]] | None = None,
        default_playlist_id: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("抖音分享口令导入")
        self.resize(620, 700)
        self._worker_thread: QThread | None = None
        self._worker: DouyinWorker | None = None
        self._rows: list[QListWidgetItem] = []
        self._playlist_options = playlist_options or []
        self._default_playlist_id = default_playlist_id
        self._build_ui()
        self._reset_steps()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(12)

        title = QLabel("抖音分享口令导入")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        hint = QLabel(
            "在本页选好目标歌单并粘贴分享口令即可一次完成。"
            "流程：分享口令 → 视频页链接 → 真实视频地址 → 导入音频（下载并转为 MP3）→ 入库。"
            "匿名导入不配置 Cookie；若抖音触发风控，对应步骤会标红并给出原因。"
        )
        hint.setObjectName("dialogHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        input_label = QLabel("分享口令或短链")
        input_label.setObjectName("fieldLabel")
        layout.addWidget(input_label)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText("粘贴抖音分享文案或短链，例如：https://v.douyin.com/xxxxx/")
        self.text_edit.setMaximumHeight(80)
        layout.addWidget(self.text_edit)

        # 目标歌单与口令在同一页选择/填写，省掉原先的独立选择步骤。
        target_label = QLabel("添加到歌单")
        target_label.setObjectName("fieldLabel")
        layout.addWidget(target_label)
        self.playlist_combo = QComboBox()
        for playlist_id, label in self._playlist_options:
            self.playlist_combo.addItem(label, playlist_id)
        index = self.playlist_combo.findData(self._default_playlist_id)
        if index >= 0:
            self.playlist_combo.setCurrentIndex(index)
        layout.addWidget(self.playlist_combo)

        self.start_button = QPushButton("开始导入")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setFixedSize(160, 40)
        layout.addWidget(self.start_button, alignment=Qt.AlignmentFlag.AlignHCenter)

        card = QFrame()
        card.setObjectName("progressCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(9)

        header = QHBoxLayout()
        self.stage_label = QLabel("准备就绪")
        self.stage_label.setObjectName("stageLabel")
        self.percent_label = QLabel("0%")
        self.percent_label.setObjectName("percentLabel")
        header.addWidget(self.stage_label, stretch=1)
        header.addWidget(self.percent_label)
        card_layout.addLayout(header)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("importProgressBar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(10)
        card_layout.addWidget(self.progress_bar)

        self.detail_label = QLabel("开始后显示当前步骤详情，下载阶段显示实时百分比")
        self.detail_label.setObjectName("stepDetail")
        self.detail_label.setWordWrap(True)
        card_layout.addWidget(self.detail_label)
        layout.addWidget(card)

        steps_label = QLabel("步骤明细")
        steps_label.setObjectName("fieldLabel")
        layout.addWidget(steps_label)

        self.step_list = QListWidget()
        self.step_list.setObjectName("stepList")
        self.step_list.setFixedHeight(204)
        layout.addWidget(self.step_list)

        self.status_label = QLabel("")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        localize_buttons(buttons)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.start_button.clicked.connect(self._on_start)

        self._progress_animation = QPropertyAnimation(self.progress_bar, b"value", self)
        self._progress_animation.setDuration(280)
        self._progress_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    @property
    def playlist_id(self) -> str:
        """歌单在导入开始前就已在对话框内选定。"""
        return str(self.playlist_combo.currentData() or "")

    # ---------- Progress presentation ----------

    def _reset_steps(self) -> None:
        self.step_list.clear()
        self._rows = []
        for index, title in enumerate(STEP_TITLES, start=1):
            item = QListWidgetItem(f"○  {index:02d}  {title}")
            item.setForeground(QColor(_STATE_COLORS["pending"]))
            self.step_list.addItem(item)
            self._rows.append(item)

    def _set_step_state(self, index: int, state: str, detail: str = "") -> None:
        if not 0 <= index < len(self._rows):
            return
        item = self._rows[index]
        text = f"{_STATE_ICONS[state]}  {index + 1:02d}  {STEP_TITLES[index]}"
        if detail:
            text += f"  ·  {detail}"
        item.setText(text)
        item.setForeground(QColor(_STATE_COLORS[state]))
        font = item.font()
        font.setBold(state == "running")
        item.setFont(font)

    def _apply_progress(self, value: int, animate: bool = True) -> None:
        value = max(0, min(100, int(value)))
        if animate:
            self._progress_animation.stop()
            self._progress_animation.setStartValue(self.progress_bar.value())
            self._progress_animation.setEndValue(value)
            self._progress_animation.start()
        else:
            self._progress_animation.stop()
            self.progress_bar.setValue(value)
        self.percent_label.setText(f"{value}%")

    def _set_bar_state(self, state: str) -> None:
        self.progress_bar.setProperty("state", state)
        self.progress_bar.style().polish(self.progress_bar)

    def _step_index(self, name: str) -> int:
        for index, prefix in enumerate(STEP_PREFIXES):
            if name.startswith(prefix):
                return index
        return -1

    # ---------- Worker wiring ----------

    def _on_start(self) -> None:
        text = self.text_edit.toPlainText().strip()
        if not text:
            warning(self, "输入为空", "请先粘贴抖音分享口令或短链")
            return
        self._reset_steps()
        self._set_step_state(0, "running")
        self.stage_label.setText("正在导入…")
        self.detail_label.setText("准备中…")
        self.status_label.setText("")
        self._set_bar_state("normal")
        self._apply_progress(0, animate=False)
        self.start_button.setEnabled(False)

        self._worker_thread = QThread(self)
        self._worker = DouyinWorker(text)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.step.connect(self._on_step)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker_thread.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_step(self, name: str, detail: str, ok: bool) -> None:
        if name.startswith("步骤5") and "正在提取音轨" in detail:
            self.stage_label.setText(f"{len(STEP_TITLES):02d} · {STEP_TITLES[-1]} 进行中")
            self.detail_label.setText(detail)
            self._apply_progress(DOWNLOAD_END, animate=False)
            return
        if name.startswith("步骤5") and _DOWNLOAD_PERCENT.search(detail):
            self.stage_label.setText(f"{len(STEP_TITLES):02d} · {STEP_TITLES[-1]} 进行中")
            self.detail_label.setText(f"正在下载音频 · {detail}")
            self._apply_progress(compute_progress(name, detail), animate=False)
            return
        if "次尝试" in detail:
            self.detail_label.setText(f"下载未成功，{detail}")
            self._apply_progress(DOWNLOAD_START, animate=False)
            return

        index = self._step_index(name)
        if index < 0:
            self.detail_label.setText(detail)
            return

        if not ok:
            self._set_step_state(index, "error", detail)
            self.stage_label.setText(f"{index + 1:02d} · {STEP_TITLES[index]} 失败")
            self.detail_label.setText(detail)
            self._set_bar_state("error")
            return

        for previous in range(index):
            if self._rows[previous].text().startswith("◐"):
                self._set_step_state(previous, "done")
        self._set_step_state(index, "done", detail)
        if index + 1 < len(STEP_TITLES):
            self._set_step_state(index + 1, "running")
        self.stage_label.setText(f"{index + 1:02d} · {STEP_TITLES[index]} 完成")
        self.detail_label.setText(detail)
        self._apply_progress(compute_progress(name, detail))

    def _on_finished(self, result: DouyinImportResult | None, error: str) -> None:
        self.start_button.setEnabled(True)
        if result is None:
            self.stage_label.setText("导入未完成")
            self.status_label.setText(f"导入失败：{error}")
            self._set_bar_state("error")
            return
        self._apply_progress(100, animate=False)
        self.stage_label.setText("导入完成")
        self.detail_label.setText(f"音频已生成：{result.audio_path.name}")
        self.status_label.setText(f"音频文件：{result.audio_path.name}")
        # 入库由主窗口的 imported 槽同步完成；成功后立即关闭本对话框，
        # 避免用户看到一个永远不会更新的中间态文案。
        self.accept()
        self.imported.emit(result)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming convention
        if self._worker_thread is not None and self._worker_thread.isRunning():
            self._worker_thread.quit()
            self._worker_thread.wait(2000)
        super().closeEvent(event)


class NewPlaylistDialog(QDialog):
    def __init__(self, parent=None, title: str = "新建歌单", initial: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(380, 150)
        self.name_edit = QLineEdit(initial)
        self.name_edit.setPlaceholderText("请输入歌单名称")

        # Custom Chinese captions instead of the standard OK/Cancel pair: at
        # least one Qt platform translates Cancel with test-locale artifacts.
        # Order matches the settings dialog: cancel left, primary action right.
        self.cancel_button = QPushButton("取消")
        self.ok_button = QPushButton("保存")
        self.ok_button.setObjectName("primaryButton")
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self._accept)
        self.cancel_button.clicked.connect(self.reject)

        row = QHBoxLayout()
        row.addWidget(QLabel("名称："))
        row.addWidget(self.name_edit)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.ok_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.addLayout(row)
        layout.addStretch(1)
        layout.addLayout(actions)

    def _accept(self) -> None:
        if not self.name_edit.text().strip():
            warning(self, "名称为空", "请输入歌单名称")
            self.name_edit.setFocus()
            return
        self.accept()

    @property
    def name(self) -> str:
        return self.name_edit.text().strip()


def ask_rename(parent, current: str) -> str | None:
    """重命名歌单。

    不用 ``QInputDialog``：它只有 OK/Cancel 两个标准按钮，必须额外改写才能
    全中文；这里直接复用 ``NewPlaylistDialog`` 的保存/取消按钮，样式也更统一。
    """
    dialog = NewPlaylistDialog(parent, title="重命名歌单", initial=current)
    if dialog.exec() != NewPlaylistDialog.DialogCode.Accepted:
        return None
    name = dialog.name
    return name if name and name != current else None


class PlaylistPickerDialog(QDialog):
    """Where an import lands.

    Playlists are the only way into the library, so every import names its
    destination. The default bucket is preselected: a user who does not care
    can press the primary key without reading the list.
    """

    def __init__(
        self,
        parent=None,
        options: list[tuple[str, str]] | None = None,
        default_id: str = "",
        title: str = "选择歌单",
        hint: str = "导入的音乐会加入所选歌单；不选就是「默认歌单」。",
        confirm_text: str = "导入",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(420, 208)

        heading = QLabel(title)
        heading.setObjectName("dialogTitle")
        description = QLabel(hint)
        description.setObjectName("dialogHint")
        description.setWordWrap(True)

        self.playlist_combo = QComboBox()
        for playlist_id, label in options or []:
            self.playlist_combo.addItem(label, playlist_id)
        index = self.playlist_combo.findData(default_id)
        if index >= 0:
            self.playlist_combo.setCurrentIndex(index)

        self.cancel_button = QPushButton("取消")
        self.ok_button = QPushButton(confirm_text)
        self.ok_button.setObjectName("primaryButton")
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch(1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.ok_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(heading)
        layout.addWidget(description)
        field = QLabel("添加到歌单")
        field.setObjectName("fieldLabel")
        layout.addWidget(field)
        layout.addWidget(self.playlist_combo)
        layout.addStretch(1)
        layout.addLayout(actions)

        self.ok_button.setEnabled(self.playlist_combo.count() > 0)

    @property
    def playlist_id(self) -> str:
        return str(self.playlist_combo.currentData() or "")


class SettingsDialog(QDialog):
    """User preferences persisted in SQLite."""

    settingsSaved = Signal(dict)
    themePreview = Signal(str)  # 主题按钮点击后立即请求切换（不等"保存"）
    backgroundPreview = Signal(str)   # 背景路径变化即时预览

    def __init__(
        self,
        parent=None,
        db_relocator: Callable[[Path], str | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(660, 720)
        # The main window supplies a relocator that can close its Library
        # connection first; standalone dialogs use the plain module helper.
        self._db_relocator = db_relocator
        self.config = load_config()
        self._build_ui()
        self._load_values()

    def _card(self, title: str) -> tuple[QFrame, QFormLayout]:
        card = QFrame()
        card.setObjectName("settingsCard")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(18, 14, 18, 16)
        outer.setSpacing(10)
        heading = QLabel(title)
        heading.setObjectName("settingsSection")
        outer.addWidget(heading)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        outer.addLayout(form)
        return card, form

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        return label

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        title = QLabel("设置")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        hint = QLabel(
            "所有设置保存在本地 SQLite 数据库中；数据库位置可在下方修改，保存时会自动迁移现有数据。"
            "修改缓存目录只影响之后的抖音导入，已有曲目路径保持不变。"
        )
        hint.setObjectName("dialogHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = transparent_panel(QWidget())
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(2, 2, 12, 2)
        content_layout.setSpacing(12)
        scroll.setWidget(content)
        layout.addWidget(scroll, stretch=1)

        # ---------- appearance ----------
        appearance_card, appearance_form = self._card("外观")
        # Theme picker: clicking a swatch previews instantly via themePreview.
        theme_row = transparent_panel(QWidget())
        theme_row_layout = QHBoxLayout(theme_row)
        theme_row_layout.setContentsMargins(0, 0, 0, 0)
        theme_row_layout.setSpacing(8)
        self.theme_group = QButtonGroup(theme_row)
        self.theme_group.setExclusive(True)
        self.theme_buttons: dict[str, QPushButton] = {}
        for info in THEMES.values():
            btn = QPushButton(f"  {info.name}")
            btn.setIcon(_swatch_icon(info.swatches))
            btn.setIconSize(QSize(30, 10))
            btn.setObjectName("themeButton")
            btn.setCheckable(True)
            btn.setToolTip(info.description)
            btn.setProperty("themeId", info.id)
            self.theme_group.addButton(btn)
            theme_row_layout.addWidget(btn)
            self.theme_buttons[info.id] = btn
        self.theme_group.buttonClicked.connect(self._on_theme_clicked)
        appearance_form.addRow(self._label("主题方案"), theme_row)
        self.theme_hint = QLabel("点击即可立即预览；点击「保存设置」后作为默认主题。")
        self.theme_hint.setObjectName("settingsHint")
        self.theme_hint.setWordWrap(True)
        appearance_form.addRow(QLabel(""), self.theme_hint)

        bg_row = transparent_panel(QWidget())
        bg_row_layout = QHBoxLayout(bg_row)
        bg_row_layout.setContentsMargins(0, 0, 0, 0)
        bg_row_layout.setSpacing(8)
        self.bg_edit = QLineEdit()
        self.bg_edit.setReadOnly(True)
        self.bg_edit.setPlaceholderText("点击「浏览…」选择背景图片或视频；留空使用默认纯色背景")
        bg_browse = QPushButton("浏览…")
        bg_browse.clicked.connect(self._browse_background)
        bg_clear = QPushButton("恢复默认")
        bg_clear.clicked.connect(self._clear_background)
        bg_row_layout.addWidget(self.bg_edit, stretch=1)
        bg_row_layout.addWidget(bg_browse)
        bg_row_layout.addWidget(bg_clear)
        appearance_form.addRow(self._label("背景"), bg_row)
        self.bg_info = QLabel("")
        self.bg_info.setObjectName("settingsHint")
        self.bg_info.setWordWrap(True)
        self.bg_info.setMaximumWidth(360)
        appearance_form.addRow(QLabel(""), self.bg_info)
        self.bg_edit.editingFinished.connect(self._refresh_bg_info)
        # 背景透明度统一固定为 20%，不再提供用户自定义选项。
        content_layout.addWidget(appearance_card)

        # ---------- storage ----------
        storage_card, storage_form = self._card("存储与缓存")
        cache_row = transparent_panel(QWidget())
        cache_row_layout = QHBoxLayout(cache_row)
        cache_row_layout.setContentsMargins(0, 0, 0, 0)
        cache_row_layout.setSpacing(8)
        self.cache_edit = QLineEdit()
        self.cache_edit.setPlaceholderText("抖音下载的 MP3 存放位置")
        cache_browse = QPushButton("浏览…")
        cache_browse.clicked.connect(self._browse_cache)
        cache_default = QPushButton("恢复默认")
        cache_default.clicked.connect(self._reset_cache)
        cache_row_layout.addWidget(self.cache_edit, stretch=1)
        cache_row_layout.addWidget(cache_browse)
        cache_row_layout.addWidget(cache_default)
        storage_form.addRow(self._label("缓存目录"), cache_row)

        self.cache_info = QLabel("")
        self.cache_info.setObjectName("settingsValue")
        self.cache_info.setMaximumWidth(360)
        storage_form.addRow(self._label("缓存占用"), self.cache_info)
        self.cache_edit.editingFinished.connect(self._refresh_cache_info)

        db_row = transparent_panel(QWidget())
        db_row_layout = QHBoxLayout(db_row)
        db_row_layout.setContentsMargins(0, 0, 0, 0)
        db_row_layout.setSpacing(8)
        self.db_edit = QLineEdit()
        self.db_edit.setPlaceholderText("SQLite 数据库文件位置")
        db_browse = QPushButton("浏览…")
        db_browse.clicked.connect(self._browse_database)
        db_default = QPushButton("恢复默认")
        db_default.clicked.connect(self._reset_database)
        db_row_layout.addWidget(self.db_edit, stretch=1)
        db_row_layout.addWidget(db_browse)
        db_row_layout.addWidget(db_default)
        storage_form.addRow(self._label("数据库位置"), db_row)

        self.db_info = QLabel("")
        self.db_info.setObjectName("settingsHint")
        self.db_info.setWordWrap(True)
        self.db_info.setMaximumWidth(360)
        storage_form.addRow(QLabel(""), self.db_info)
        self.db_edit.editingFinished.connect(self._refresh_db_info)

        data_value = QLabel()
        data_value.setObjectName("settingsValue")
        data_value.setToolTip(str(DATA_DIR))
        metrics = QFontMetrics(data_value.font())
        data_value.setText(metrics.elidedText(str(DATA_DIR), Qt.TextElideMode.ElideMiddle, 340))
        data_value.setMaximumWidth(360)
        storage_form.addRow(self._label("数据目录"), data_value)
        data_hint = QLabel("封面与临时文件位于数据目录；可用环境变量 MUSICPLAYER_DATA_DIR 覆盖。数据库位置可单独修改，默认就在该目录下。")
        data_hint.setObjectName("settingsHint")
        data_hint.setWordWrap(True)
        data_hint.setMaximumWidth(360)
        storage_form.addRow(QLabel(""), data_hint)
        content_layout.addWidget(storage_card)

        # ---------- download ----------
        download_card, download_form = self._card("下载与转码")
        ffmpeg_row = transparent_panel(QWidget())
        ffmpeg_layout = QHBoxLayout(ffmpeg_row)
        ffmpeg_layout.setContentsMargins(0, 0, 0, 0)
        ffmpeg_layout.setSpacing(8)
        self.ffmpeg_edit = QLineEdit()
        self.ffmpeg_edit.setPlaceholderText("留空则用内置 ffmpeg，或填写完整路径")
        ffmpeg_browse = QPushButton("浏览…")
        ffmpeg_browse.clicked.connect(self._browse_ffmpeg)
        ffmpeg_layout.addWidget(self.ffmpeg_edit, stretch=1)
        ffmpeg_layout.addWidget(ffmpeg_browse)
        download_form.addRow(self._label("FFmpeg"), ffmpeg_row)
        ffmpeg_hint = QLabel(
            "程序已内置 LGPL 版 ffmpeg，无需安装即可转码；此处仅在想改用"
            "自定义 ffmpeg 时才需要填写。"
        )
        ffmpeg_hint.setObjectName("settingsHint")
        ffmpeg_hint.setWordWrap(True)
        ffmpeg_hint.setMaximumWidth(380)
        download_form.addRow(QLabel(""), ffmpeg_hint)

        self.request_spin = QSpinBox()
        self.request_spin.setRange(5, 600)
        self.request_spin.setSuffix(" 秒")
        download_form.addRow(self._label("解析超时"), self.request_spin)

        self.download_spin = QSpinBox()
        self.download_spin.setRange(10, 1800)
        self.download_spin.setSuffix(" 秒")
        download_form.addRow(self._label("下载超时"), self.download_spin)

        self.download_retry_spin = QSpinBox()
        self.download_retry_spin.setRange(1, 10)
        self.download_retry_spin.setSuffix(" 次")
        download_form.addRow(self._label("下载重试"), self.download_retry_spin)

        self.fetch_retry_spin = QSpinBox()
        self.fetch_retry_spin.setRange(1, 10)
        self.fetch_retry_spin.setSuffix(" 次")
        download_form.addRow(self._label("解析重试"), self.fetch_retry_spin)

        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["1080p", "720p", "540p"])
        download_form.addRow(self._label("优先清晰度"), self.quality_combo)
        content_layout.addWidget(download_card)

        # ---------- aggregated search ----------
        search_card, search_form = self._card("聚合搜索")
        self.search_timeout_spin = QSpinBox()
        self.search_timeout_spin.setRange(3, 120)
        self.search_timeout_spin.setSuffix(" 秒")
        search_form.addRow(self._label("搜索超时"), self.search_timeout_spin)

        self.search_limit_spin = QSpinBox()
        self.search_limit_spin.setRange(5, 200)
        self.search_limit_spin.setSuffix(" 首")
        search_form.addRow(self._label("每源最多"), self.search_limit_spin)
        search_hint = QLabel(
            "音源来自程序目录下的 sources 文件夹，启动时自动加载，无需手动配置。"
            "搜索由客户端内置完成，试听或下载时再由音源脚本解析播放地址。"
        )
        search_hint.setObjectName("settingsHint")
        search_hint.setWordWrap(True)
        search_hint.setMaximumWidth(380)
        search_form.addRow(QLabel(""), search_hint)
        content_layout.addWidget(search_card)

        # ---------- about / version ----------
        from version import CODENAME, RELEASE_DATE, RELEASES, VERSION
        about_card, about_form = self._card("关于")
        about_form.addRow(self._label("版本"), QLabel(f"v{VERSION} · {CODENAME}"))
        about_form.addRow(self._label("发布日期"), QLabel(RELEASE_DATE))
        notes = "\n".join(f"• {note}" for note in RELEASES[0]["notes"])
        notes_label = QLabel(notes)
        notes_label.setObjectName("settingsHint")
        notes_label.setWordWrap(True)
        about_form.addRow(self._label("本次更新"), notes_label)
        copyright_label = QLabel(COPYRIGHT_NOTICE)
        copyright_label.setObjectName("settingsHint")
        copyright_label.setWordWrap(True)
        about_form.addRow(self._label("版权说明"), copyright_label)
        content_layout.addWidget(about_card)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("保存设置")
        self.save_button.setObjectName("primaryButton")
        self.save_button.clicked.connect(self.accept)
        button_row.addWidget(self.cancel_button)
        button_row.addWidget(self.save_button)
        layout.addLayout(button_row)

    def _on_theme_clicked(self, button: QPushButton) -> None:
        """预览主题：点击即发信号，由主窗口立即应用（不等保存）。"""
        theme_id = str(button.property("themeId") or "")
        if theme_id in THEMES:
            self.themePreview.emit(theme_id)

    def _load_values(self) -> None:
        config = self.config
        selected = str(config.get("theme", current_theme_id()) or current_theme_id())
        if selected not in self.theme_buttons:
            selected = current_theme_id()
        self.theme_buttons[selected].setChecked(True)
        self.bg_edit.setText(str(config.get("background_image", "") or ""))
        self.cache_edit.setText(str(get_cache_dir()))
        self.db_edit.setText(str(current_db_file()))
        self.ffmpeg_edit.setText(str(config.get("ffmpeg_path", "ffmpeg")))
        self.request_spin.setValue(int(config.get("request_timeout_seconds", 45)))
        self.download_spin.setValue(int(config.get("download_timeout_seconds", 120)))
        self.download_retry_spin.setValue(int(config.get("douyin_download_retries", 3)))
        self.fetch_retry_spin.setValue(int(config.get("douyin_fetch_retries", 3)))
        quality = str(config.get("douyin_quality", "1080p"))
        index = self.quality_combo.findText(quality)
        self.quality_combo.setCurrentIndex(max(0, index))
        self.search_timeout_spin.setValue(int(config.get("search_timeout_seconds", 15)))
        self.search_limit_spin.setValue(int(config.get("search_limit_per_source", 30)))
        self._refresh_bg_info()
        self._refresh_cache_info()
        self._refresh_db_info()

    def _refresh_bg_info(self) -> None:
        from ui.background import is_video
        raw = self.bg_edit.text().strip()
        if not raw:
            self.bg_info.setText("未设置背景；使用默认纯色背景。")
            return
        try:
            exists = Path(raw).expanduser().is_file()
        except OSError:
            exists = False
        if not exists:
            self.bg_info.setText("文件不存在或无法读取；保存时将忽略该路径。")
        elif is_video(raw):
            self.bg_info.setText("视频背景将静音无限循环播放，选择后立即预览，保存后生效。")
        else:
            self.bg_info.setText("图片将以半透明覆盖整个窗口，选择后立即预览，保存后生效。")

    def _browse_background(self) -> None:
        from ui.background import SUPPORTED_FORMATS

        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择背景图片或视频",
            self.bg_edit.text().strip(),
            SUPPORTED_FORMATS,
        )
        if selected:
            self.bg_edit.setText(selected)
            self._refresh_bg_info()
            self.backgroundPreview.emit(selected)

    def _clear_background(self) -> None:
        self.bg_edit.setText("")
        self._refresh_bg_info()
        self.backgroundPreview.emit("")

    def _refresh_db_info(self) -> None:
        raw = self.db_edit.text().strip()
        try:
            path = Path(raw).expanduser().resolve() if raw else current_db_file()
        except OSError:
            self.db_info.setText("路径无法解析，请检查输入。")
            return
        if path == current_db_file():
            size = f" · {path.stat().st_size / 1024 / 1024:.1f} MB" if path.is_file() else ""
            self.db_info.setText(f"当前使用的数据库：{path}{size}")
        elif path.exists():
            self.db_info.setText("目标已存在；为避免覆盖已有数据，保存时会被拒绝。")
        else:
            self.db_info.setText("保存时现有数据会自动迁移到该位置，原文件不再使用。")

    def _refresh_cache_info(self) -> None:
        raw = self.cache_edit.text().strip()
        path = Path(raw).expanduser() if raw else CACHE_DIR
        if not path.is_dir():
            self.cache_info.setText("目录尚不存在，保存时将自动创建。")
            return
        files = [entry for entry in path.glob("*.mp3") if entry.is_file()]
        megabytes = sum(entry.stat().st_size for entry in files) / 1024 / 1024
        self.cache_info.setText(f"当前缓存：{len(files)} 个音频 · {megabytes:.1f} MB")

    def _browse_cache(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择缓存目录", self.cache_edit.text() or str(get_cache_dir())
        )
        if selected:
            self.cache_edit.setText(selected)
            self._refresh_cache_info()

    def _reset_cache(self) -> None:
        self.cache_edit.setText(str(CACHE_DIR))
        self._refresh_cache_info()

    def _browse_database(self) -> None:
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "选择数据库文件",
            self.db_edit.text() or str(current_db_file()),
            "SQLite 数据库 (*.db *.sqlite *.sqlite3);;所有文件 (*)",
            "",
            QFileDialog.Option.DontConfirmOverwrite,
        )
        if selected:
            self.db_edit.setText(selected)
            self._refresh_db_info()

    def _reset_database(self) -> None:
        # Import at call time so a redirected default (tests, portable
        # installs) is respected instead of a stale module-level copy.
        from config.settings import DB_FILE

        self.db_edit.setText(str(DB_FILE))
        self._refresh_db_info()

    def _browse_ffmpeg(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择 FFmpeg 可执行文件", "", "可执行文件 (*.exe);;所有文件 (*)"
        )
        if selected:
            self.ffmpeg_edit.setText(selected)

    def accept(self) -> None:  # noqa: N802 - Qt naming convention
        raw_cache = self.cache_edit.text().strip()
        if not raw_cache:
            warning(self, "缓存目录为空", "请指定缓存目录，或点击「恢复默认」")
            return
        cache_path = Path(raw_cache).expanduser()
        if cache_path.exists() and not cache_path.is_dir():
            warning(self, "路径不可用", "缓存目录指向了一个文件，请改为文件夹路径")
            return
        try:
            cache_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            warning(self, "无法创建目录", f"缓存目录无法创建：{exc}")
            return

        ffmpeg = self.ffmpeg_edit.text().strip()
        if not ffmpeg:
            warning(self, "FFmpeg 路径为空", "请填写 ffmpeg 或选择可执行文件")
            return
        if ("/" in ffmpeg or "\\" in ffmpeg) and not Path(ffmpeg).expanduser().is_file():
            warning(self, "FFmpeg 不存在", f"找不到文件：{ffmpeg}")
            return

        raw_db = self.db_edit.text().strip()
        if not raw_db:
            warning(self, "数据库位置为空", "请填写数据库文件路径，或点击「恢复默认」")
            return
        try:
            db_target = Path(raw_db).expanduser().resolve()
        except OSError:
            warning(self, "路径不可用", "数据库路径无法解析，请检查输入")
            return
        if db_target.is_dir():
            warning(self, "路径不可用", "数据库位置需要是文件名，不能是文件夹")
            return
        # The current database naturally exists; only a *new* destination
        # needs the overwrite guard.
        if db_target != current_db_file():
            if db_target.exists():
                warning(self, "目标文件已存在", "为避免覆盖已有数据，请为数据库选择其他位置")
                return
            if db_target.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
                warning(self, "文件名不推荐", "数据库文件建议使用 .db 后缀，请修改后保存")
                return
            from config.settings import switch_database_file

            relocator = self._db_relocator or switch_database_file
            error = relocator(db_target)
            if error:
                warning(self, "数据库位置修改失败", error)
                return

        checked = self.theme_group.checkedButton()
        theme_id = str(checked.property("themeId") or current_theme_id()) if checked else current_theme_id()
        updates = dict(self.config)
        updates.update(
            {
                "theme": theme_id,
                "background_image": self.bg_edit.text().strip(),
                "cache_dir": str(cache_path),
                "db_file": str(current_db_file()),
                "ffmpeg_path": ffmpeg,
                "request_timeout_seconds": self.request_spin.value(),
                "download_timeout_seconds": self.download_spin.value(),
                "douyin_download_retries": self.download_retry_spin.value(),
                "douyin_fetch_retries": self.fetch_retry_spin.value(),
                "douyin_quality": self.quality_combo.currentText(),
                "search_timeout_seconds": self.search_timeout_spin.value(),
                "search_limit_per_source": self.search_limit_spin.value(),
            }
        )
        save_config(updates)
        self.settingsSaved.emit(updates)
        super().accept()
