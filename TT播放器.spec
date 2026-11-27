# -*- mode: python ; coding: utf-8 -*-
"""TT 播放器打包配置（目录模式 / onedir）。

产物结构::

    dist/TT播放器/
        TT播放器.exe
        _internal/          运行库与 Python 依赖
        sources/            音源脚本（见打包脚本，位于 exe 同级，可直接增删）
        runtime/node.exe    可选，随包附带时解析音源不依赖系统 Node

数据库不在此产物中：首次启动于用户数据目录（MUSICPLAYER_DATA_DIR /
%APPDATA%/MusicPlayer）按表增量创建，升级不覆盖用户数据。

产品版本号唯一定义在根目录 version.py（main.py / ui 模块均 import 它，
PyInstaller 会自动收集）；发版时只需改该文件，无需改本配置。

不使用 ``--onefile``：单文件模式下每次启动都要把约 60 MB 依赖解压到临时
目录，首次启动明显变慢，且 ``sources`` 无法放在 exe 旁边供用户维护。
"""

import os

ROOT = SPECPATH

# 数据库文件绝不随包分发：它在首次运行时于用户数据目录按需创建，
# 升级只覆盖程序文件，用户已有的 musicplayer.db 不受影响。
DATAS = [
    # LX 音源宿主脚本：core/lx_proc.py 通过 __file__ 定位，
    # 目录模式下解析为 _internal/core/js/lx_host.js。
    (os.path.join(ROOT, "core", "js", "lx_host.js"), os.path.join("core", "js")),
]
for source, _target in DATAS:
    assert not str(source).lower().endswith((".db", ".db-wal", ".db-shm")), (
        "打包数据不得包含 SQLite 数据库文件"
    )

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=DATAS,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
