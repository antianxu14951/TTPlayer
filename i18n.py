from __future__ import annotations

from PySide6.QtCore import QLibraryInfo, QTranslator
from PySide6.QtWidgets import QApplication

# Keep a module-level reference: Qt does not take ownership of the translator, so
# a local one would be garbage-collected right after install() and translations
# would silently stop working.
_TRANSLATORS: list[QTranslator] = []

# qtbase carries every widget string (file dialog buttons, line-edit context menu,
# standard button captions); qt_zh_CN is the older aggregate bundle used as a
# fallback when a Qt build has no qtbase-specific translation.
_CANDIDATES = ("qtbase_zh_CN", "qt_zh_CN")


def install_chinese_translation(app: QApplication) -> bool:
    """Translate Qt's built-in strings to Simplified Chinese.

    Our own dialogs set Chinese captions explicitly, but widgets we do not draw
    ourselves (``QFileDialog``, the line-edit right-click menu, message-box
    standard buttons) would otherwise leak English. Returns whether a
    translation file was loaded; the app still works in Chinese without it.
    """
    if _TRANSLATORS:
        return True
    translations_path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    for name in _CANDIDATES:
        translator = QTranslator(app)
        if translator.load(name, translations_path):
            app.installTranslator(translator)
            _TRANSLATORS.append(translator)
            return True
    return False
