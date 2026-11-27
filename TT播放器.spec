# -*- mode: python ; coding: utf-8 -*-
"""TT 播放器打包配置（目录模式 / onedir，引导器架构）。

产物结构::

    dist/TT播放器/
        TT播放器.exe        引导器：入口是 update.py，含 loading 动画与升级逻辑
        _internal/
            app/            业务代码（纯源码，可被升级包整体替换）
                main.py / ui/ / core/ / config/ / i18n.py / version.py
                app_version.txt
            …               PySide6、Python DLL 等运行时（升级不触碰）
        sources/            音源脚本（打包脚本复制，用户可自行增删）
        runtime/node.exe    可选，随包附带时解析音源不依赖系统 Node

升级模型（见 update.py）：
    exe 只含引导器，永不更新 → 不存在"运行中 exe 覆盖自己"的锁死问题。
    升级 = 替换 _internal/app/，发生在 import main 之前，无文件锁。

数据库不在此产物中：首次启动于用户数据目录（MUSICPLAYER_DATA_DIR /
%APPDATA%/MusicPlayer）按表增量创建，升级不覆盖用户数据。

版本号唯一定义在根目录 version.py；打包时自动写一份 app_version.txt
进 _internal/app/，供引导器做版本对比。发版流程见 tools/release.py。
"""

import os
import re
from pathlib import Path

ROOT = SPECPATH

# ---------------------------------------------------------------------------
# 版本：从 version.py 提取，写入 _internal/app/app_version.txt
# ---------------------------------------------------------------------------
_version_text = (Path(ROOT) / "version.py").read_text(encoding="utf-8")


def _spec_int(name: str) -> int:
    m = re.search(rf"^{name}\s*=\s*(\d+)", _version_text, re.M)
    return int(m.group(1)) if m else 0


APP_VERSION = f"{_spec_int('MAJOR')}.{_spec_int('MINOR')}.{_spec_int('PATCH')}"

_marker = Path(ROOT) / "build" / "app_version.txt"
_marker.parent.mkdir(parents=True, exist_ok=True)
_marker.write_text(APP_VERSION + "\n", encoding="utf-8")


def _collect_app_sources() -> list[tuple[str, str]]:
    """把业务源码收集为 (src_file, dest_dir) 对，供 Analysis.datas 使用。

    PyInstaller datas 语义（实测 6.22）：src 为文件时 dest 一律视为**目录**，
    文件名自动追加。因此：
      * 单文件条目  dest="app"            → 落在 app/<文件名>
      * 目录内文件  dest="app/<子目录>"   → 落在 app/<子目录>/<文件名>
    以纯 .py 源码形式分发（不进 PYZ）：升级包只需覆盖 _internal/app/，
    引导器在运行时把该目录插到 sys.path[0]。
    """
    pairs: list[tuple[str, str]] = []
    for name in ("config", "core", "ui"):
        src = Path(ROOT) / name
        for py in sorted(src.rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            # dest = "app/<相对父目录>"；顶层文件正是 "app"
            rel_parent = py.relative_to(ROOT).parent
            pairs.append((str(py), os.path.join("app", str(rel_parent))))
    for name in ("i18n.py", "version.py", "main.py"):
        pairs.append((str(Path(ROOT) / name), "app"))
    return pairs


APP_DATAS: list[tuple[str, str]] = list(_collect_app_sources())
APP_DATAS.append(
    # 打包时现写版本标记，引导器读它与 GitHub Release 对比
    (str(_marker), "app")
)
APP_DATAS.append(
    # LX 音源宿主脚本：core/lx_proc.py 通过 __file__ 定位。
    (os.path.join(ROOT, "core", "js", "lx_host.js"), os.path.join("app", "core", "js")),
)
for source, _target in APP_DATAS:
    assert not str(source).lower().endswith((".db", ".db-wal", ".db-shm")), (
        "打包数据不得包含 SQLite 数据库文件"
    )

# 业务模块必须只以 _internal/app 源码形式存在，绝不进 exe 的 PYZ：
# update.py 用 importlib 动态导入，但 PyInstaller 有时仍会顺着隐式引用
# 收集，这里显式排除做双保险。
BUSINESS_EXCLUDES = [
    "main", "version", "i18n",
    "config", "config.settings",
    "core", "ui",
    "core.database", "core.player", "core.library", "core.metadata",
    "core.models", "core.music_source", "core.native_search",
    "core.douyin", "core.ffmpeg_bin", "core.lx_bootstrap",
    "core.lx_proc", "core.lx_source",
    "ui.main_window", "ui.background", "ui.branding", "ui.dialogs",
    "ui.formatting", "ui.icons", "ui.queue_popup", "ui.queue_view",
    "ui.search_page", "ui.texts", "ui.theme", "ui.themes", "ui.toast",
]

# 业务的第三方依赖：静态分析看不到动态 import 的 main，必须显式列出，
# 作为运行时组件打进 _internal/（它们属"运行时"，不随版本升级变化，
# 符合"精简包只替换业务源码"的模型）。标准库按需列风险高的几个。
RUNTIME_HIDDEN_IMPORTS = [
    "sqlite3",
    "uuid",
    "requests",
    "mutagen", "mutagen.flac", "mutagen.mp3", "mutagen.mp4",
    "mutagen.id3", "mutagen._vorbis",
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    "PySide6.QtMultimedia", "PySide6.QtNetwork",
]

a = Analysis(
    [os.path.join(ROOT, "update.py")],     # 引导器是唯一入口
    pathex=[ROOT],
    binaries=[],
    datas=APP_DATAS,
    hiddenimports=RUNTIME_HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=BUSINESS_EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TT播放器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(ROOT, "assets", "logo.ico")],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TT播放器",
)
