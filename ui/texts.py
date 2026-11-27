"""All user-facing copy in Simplified Chinese.

Two jobs live here:

1. Keep wording in one place so confirmations, menus and settings share one
   voice instead of drifting apart copy by copy.
2. Guarantee no English button ever reaches the screen. Qt labels its own
   standard buttons "OK" / "Save" / "Cancel" / "Close" / "Yes" / "No"; the
   helpers below rename them in one place so a new dialog cannot accidentally
   introduce English. Brand names (FFmpeg, MP3) and JSON field paths keep their
   proper spelling -- only interface chrome is translated.

Reminders (``warning`` / ``show_info``) are green, buttonless toasts that fade
away after ``HINT_AUTO_CLOSE_MS`` -- they never grab focus or play a system
sound. Confirmations (``ask_confirm`` / ``ask_danger``) stay modal because the
user's decision changes what the program does.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialogButtonBox, QMessageBox, QPushButton, QWidget

from ui.toast import Toast

# Reminders (information / warning) never wait for a click: they close
# themselves after this delay. Confirmation dialogs are deliberately not
# affected - a destructive action still needs an explicit human answer.
HINT_AUTO_CLOSE_MS = 3000

# Every standard button shows a Chinese caption. Buttons absent from this map
# keep Qt's native text rather than becoming blank.
STANDARD_BUTTON_TEXT: dict[str, str] = {
    "OK": "确定",
    "SAVE": "保存",
    "SAVEALL": "全部保存",
    "OPEN": "打开",
    "YES": "确定",
    "YESTOALL": "全部确定",
    "NO": "取消",
    "NOTOALL": "全部取消",
    "CANCEL": "取消",
    "CLOSE": "关闭",
    "ABORT": "中止",
    "RETRY": "重试",
    "IGNORE": "忽略",
}


def localize_buttons(buttons: QDialogButtonBox) -> None:
    """把按钮框中的标准按钮统一改成中文。"""
    for button in buttons.buttons():
        if not isinstance(button, QPushButton):
            continue
        standard = buttons.standardButton(button)
        text = STANDARD_BUTTON_TEXT.get(standard.name.upper())
        if text:
            button.setText(text)


def _show_transient(
    parent: QWidget | None,
    icon: QMessageBox.Icon,
    title: str,
    text: str,
    detail: str,
    buttons: QMessageBox.StandardButton,
) -> int:
    """显示非阻塞的绿色 Toast，无按钮、无系统音效，数秒后自动淡出。

    只有提醒级别走这里；需要用户抉择的确认框仍使用 exec() 阻塞等待。
    ``icon`` / ``buttons`` 保留在签名里是为了兼容既有调用方，Toast 自身
    不渲染图标或按钮——提醒不需要用户操作。
    """
    Toast(parent, title, text, detail, HINT_AUTO_CLOSE_MS)
    return 0


def warning(parent, title: str, text: str, detail: str = "") -> int:
    """中文警告提醒：显示 3 秒后自动关闭，无需手动确认。"""
    return _show_transient(
        parent,
        QMessageBox.Icon.Warning,
        title,
        text,
        detail,
        QMessageBox.StandardButton.Ok,
    )


def _message_box(
    parent: QWidget | None,
    icon: QMessageBox.Icon,
    title: str,
    text: str,
    detail: str,
) -> QMessageBox:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    if detail:
        box.setInformativeText(detail)
    return box


def show_info(
    parent: QWidget | None,
    title: str,
    text: str,
    detail: str = "",
    buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
) -> int:
    """中文信息提醒：默认 3 秒自动关闭，不要求用户点确认。

    若调用方显式要求非默认按钮组合，说明这是一次需要选择的对话，
    仍按原来的模态方式执行，避免误把确认框自动关掉。
    """
    if buttons != QMessageBox.StandardButton.Ok:
        box = _message_box(parent, QMessageBox.Icon.Information, title, text, detail)
        box.setStandardButtons(buttons)
        localize_buttons(box)
        return box.exec()
    return _show_transient(
        parent,
        QMessageBox.Icon.Information,
        title,
        text,
        detail,
        buttons,
    )


def ask_confirm(
    parent: QWidget | None,
    title: str,
    text: str,
    detail: str = "",
    *,
    confirm_text: str = "确定",
    cancel_text: str = "取消",
    default_cancel: bool = True,
) -> bool:
    """中文确认框：返回用户是否确认。

    使用自定义文案而不是 Yes/No，所以按钮始终是可读的中文，也不会因为系统
    语言不同而变回英文。
    """
    box = _message_box(parent, QMessageBox.Icon.Question, title, text, detail)
    confirm = box.addButton(confirm_text, QMessageBox.ButtonRole.YesRole)
    cancel = box.addButton(cancel_text, QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(cancel if default_cancel else confirm)
    box.exec()
    return box.clickedButton() is confirm


def ask_danger(
    parent: QWidget | None,
    title: str,
    text: str,
    detail: str = "",
    *,
    confirm_text: str = "确定删除",
    cancel_text: str = "取消",
) -> bool:
    """危险操作确认框：默认焦点在取消，避免误触。"""
    box = _message_box(parent, QMessageBox.Icon.Warning, title, text, detail)
    confirm = box.addButton(confirm_text, QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton(cancel_text, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is confirm
