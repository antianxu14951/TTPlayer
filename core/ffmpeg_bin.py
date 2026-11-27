"""Locate the FFmpeg executable, preferring the copy shipped with the app.

The binary lives in ``runtime/ffmpeg.exe`` next to the program (both a source
checkout and an onedir PyInstaller build use this layout). Users who have never
installed FFmpeg therefore work out of the box. The resolution order is:

1. the bundled ``runtime/ffmpeg`` (so behaviour is identical everywhere);
2. an explicit path from settings (``ffmpeg_path``);
3. ``ffmpeg`` on the system PATH.

Every consumer should call :func:`resolve_ffmpeg` instead of reaching for
``shutil.which`` directly, so the bundled copy is always considered first.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from config.settings import load_config

_EXE_NAME = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


def no_console_kwargs() -> dict[str, Any]:
    """Popen options that stop FFmpeg flashing a black console window.

    GUI builds are launched without a console, but a bundled ``ffmpeg.exe``
    still creates its own console on Windows unless it is started with
    ``CREATE_NO_WINDOW`` (and the startup-info hide flag for wrapper scripts).
    Non-Windows platforms need nothing.
    """
    if os.name != "nt":
        return {}
    kwargs: dict[str, Any] = {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    }
    startupinfo_type = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_type is not None:
        info = startupinfo_type()
        info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        info.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = info
    return kwargs


def bundled_candidates() -> list[Path]:
    """Directories where a shipped FFmpeg may live, in priority order."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "runtime" / _EXE_NAME)
        candidates.append(exe_dir / _EXE_NAME)
        candidates.append(
            Path(getattr(sys, "_MEIPASS", ".") or ".") / "runtime" / _EXE_NAME
        )
    root = Path(__file__).resolve().parent.parent
    candidates.append(root / "runtime" / _EXE_NAME)
    candidates.append(Path.cwd() / "runtime" / _EXE_NAME)
    return candidates


def bundled_ffmpeg() -> Path | None:
    """Return the shipped FFmpeg path if it exists, else ``None``."""
    for candidate in bundled_candidates():
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def resolve_ffmpeg() -> str:
    """Return a usable FFmpeg executable path.

    Bundled first, then the configured path, then the system PATH. Raises
    ``FileNotFoundError`` with a clear message if none can be found.
    """
    bundled = bundled_ffmpeg()
    if bundled is not None:
        return str(bundled)

    configured = str(load_config().get("ffmpeg_path", "ffmpeg") or "ffmpeg").strip()
    candidate = Path(configured).expanduser()
    if configured and candidate.is_file():
        return str(candidate)
    found = shutil.which(configured) or shutil.which("ffmpeg")
    if found:
        return found
    raise FileNotFoundError(
        "未找到 ffmpeg。项目内置的 runtime/ffmpeg 缺失，系统 PATH 中也未安装，"
        "请在设置中指定 ffmpeg 路径。"
    )
