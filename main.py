from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from config.settings import close_db, ensure_dirs
from i18n import install_chinese_translation
from ui.branding import BRAND_NAME, app_icon
from ui.main_window import MainWindow
from ui.theme import install_button_cursors
from version import VERSION


def main() -> None:
    ensure_dirs()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName(BRAND_NAME)
    app.setApplicationDisplayName(BRAND_NAME)
    app.setApplicationVersion(VERSION)
    app.setWindowIcon(app_icon())
    install_chinese_translation(app)
    install_button_cursors(app)
    window = MainWindow()
    window.show()
    exit_code = app.exec()
    close_db()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
