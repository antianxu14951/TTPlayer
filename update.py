"""TT 播放器引导器 —— 打包后 exe 的唯一入口，本文件永不更新。

设计约束（为什么必须由本文件承担升级）：
    PyInstaller onedir 产物中，exe 只嵌入本引导器；业务代码以纯源码形式
    放在 ``_internal/app/``。升级 = 整体替换 ``_internal/app/``。本文件
    不参与更新包，且替换发生在 ``import main`` 之前——业务模块尚未加载，
    没有任何文件被占用，因此彻底绕开"运行中的 exe 覆盖自己"的锁死问题。

    因此本文件对业务模块一律使用**动态导入**（importlib）：PyInstaller 的
    静态分析无法解析，业务代码绝不会被塞进 exe——exe 才能真正"永不变化"。
    spec 侧另有 excludes 双保险。

启动流程::

    exe → update.py
        ├─ --launch 参数 ────────────── 直接启动（升级重启/跳过检查，防循环）
        ├─ --check  参数 ────────────── 命令行自检：打印 JSON 决策后退出
        └─ 引导外壳（loading 动画）→ 后台线程查 GitHub Release
              ├─ 成功且 remote > local → 下载精简包 → 原子替换 _internal/app/
              ├─ 成功但已是最新
              └─ 失败（网络/超时/解析/磁盘）
              ↓ 三条路殊途同归
          以 --launch 重启自身 → 全新进程 → 动态 import main → 主窗口

降级铁律：任何一步出错都不允许拦截用户进入播放器；最坏情形只是
"这次没升成"，绝不能是"程序打不开"。

主程序交接为什么走"重启新进程"而不是同进程 import main：
    main.py（保持不动）会自行 QApplication(sys.argv)；引导外壳已经创建过
    一个实例，同进程二次创建会被 Qt 拒绝。新进程干净、确定、无副作用。
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

REPO_API = "https://api.github.com/repos/antianxu14951/TTPlayer/releases/latest"
CHECK_TIMEOUT = 5.0          # 版本检查超时（秒），超时即降级直启
DOWNLOAD_TIMEOUT = 60.0      # 下载 socket 超时（秒）
APP_DIR_NAME = "app"         # _internal/app：业务代码目录
USER_AGENT = "TTPlayer-Updater"
BRAND_NAME = "TT播放器"       # 引导外壳自持，避免静态依赖 ui.branding

# ---------------------------------------------------------------- path layout


def exe_dir() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent


def internal_dir() -> Path:
    base = getattr(sys, "_MEIPASS", "")
    if base:
        return Path(base)
    return exe_dir() / "_internal"


def app_root() -> Path:
    """业务代码目录（含 main.py / ui / core）。"""
    if getattr(sys, "frozen", False):
        return internal_dir() / APP_DIR_NAME
    return Path(__file__).resolve().parent      # 源码运行：项目根


def ensure_app_on_path() -> None:
    root = str(app_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def local_version() -> str:
    """读取 app_version.txt；源码运行则回退 version.py。"""
    try:
        marker = app_root() / "app_version.txt"
        if marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError:
        pass
    try:
        ensure_app_on_path()
        version = importlib.import_module("version")
        return str(version.VERSION)
    except Exception:
        return "0.0.0"


# ----------------------------------------------------------------- versioning


def parse_semver(text: str) -> tuple[int, int, int] | None:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", str(text).strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def is_newer(remote: str, local: str) -> bool:
    r, l = parse_semver(remote), parse_semver(local)
    return bool(r and l and r > l)


# ------------------------------------------------------------------ networking


def fetch_latest_release() -> dict | None:
    """查询最新 Release；返回 {tag, notes, url, size}，无可用精简包则 None。"""
    req = urllib.request.Request(
        REPO_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return None
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name.endswith(".zip") and "lite" in name.lower() and url:
            return {
                "tag": tag,
                "notes": str(data.get("body") or ""),
                "url": url,
                "size": int(asset.get("size") or 0),
            }
    return None


def download(url: str, dest: Path, progress=None) -> None:
    """流式下载到 dest；progress(0..1) 可选回调。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, \
            open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0) or None
        done = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(min(1.0, done / total))


def apply_update(zip_path: Path) -> None:
    """把精简包内容整体替换到 _internal/app/。

    精简包内层为 ``app/…`` 目录。先解压到同级临时目录再换名切入：
    rename 同卷原子性保证不会出现半新半旧；失败时把旧目录换回去。
    """
    target = app_root()
    stage = target.parent / f"app_update_{os.getpid()}"
    old = target.parent / f"app_old_{os.getpid()}"
    for p in (stage, old):
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(stage)
        source = stage / APP_DIR_NAME if (stage / APP_DIR_NAME).is_dir() else stage
        if not (source / "main.py").is_file():
            raise RuntimeError("更新包内容不完整：缺少 main.py")
        if target.exists():
            os.replace(target, old)
        os.replace(source, target)
    finally:
        shutil.rmtree(old, ignore_errors=True)
        shutil.rmtree(stage, ignore_errors=True)


def relaunch() -> None:
    """以 --launch 重启自身：全新进程，跳过升级检查直接进主程序。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    subprocess.Popen(
        [sys.executable, "--launch"],
        cwd=str(exe_dir()),
        creationflags=flags,
        close_fds=True,
    )


# ------------------------------------------------------------------- decision


def decide() -> dict:
    """纯逻辑决策（无 UI、无副作用），供外壳与 --check 共用。

    返回 {"action": "launch"|"update", "tag": str, "notes": str}
    """
    release = fetch_latest_release()
    if release and is_newer(release["tag"], local_version()):
        return {
            "action": "update",
            "tag": release["tag"],
            "notes": release["notes"],
            "url": release["url"],
            "size": release["size"],
        }
    return {"action": "launch", "tag": release["tag"] if release else "",
            "notes": "", "url": "", "size": 0}


def perform_update(plan: dict, progress=None) -> None:
    zip_path = Path(tempfile.gettempdir()) / f"tt_update_{plan['tag']}.zip"
    download(plan["url"], zip_path, progress)
    apply_update(zip_path)


def check_cli() -> int:
    """命令行自检：--check 打印决策 JSON。"""
    try:
        plan = decide()
        print(json.dumps(
            {"local": local_version(), **plan}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"action": "launch", "error": str(exc),
                          "local": local_version()}, ensure_ascii=False))
        return 1


# ------------------------------------------------------------------- UI shell


def run_boot_shell() -> int:
    """带 loading 动画的升级外壳；结束时必然 relaunch(--launch) 新进程。

    动画自绘（品牌色弧线 + 进度条），不 import 任何业务模块——exe 内
    不应存在业务代码（见模块 docstring 的静态分析说明）。
    """
    from PySide6.QtCore import QRect, QRectF, Qt, QTimer, Signal
    from PySide6.QtGui import QColor, QPainter, QRadialGradient
    from PySide6.QtWidgets import QApplication, QDialog

    app = QApplication(sys.argv[:1])
    app.setApplicationName(BRAND_NAME)

    shell = QDialog()
    shell.setWindowTitle(BRAND_NAME)
    shell.setWindowFlags(
        Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
    )
    shell.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    shell.setFixedSize(320, 320)

    class _Done(QDialog):
        """跨线程收尾：工作线程 emit，主线程队列投递 accept。"""

        finished = Signal()

        def __init__(self) -> None:
            super().__init__()
            self.finished.connect(self.accept)

    shell = _Done()

    state = {"phase": "check", "spin": 0.0, "progress": 0.0}
    PHASE_TEXT = {
        "check": "正在检查更新…",
        "found": "发现新版本，正在下载…",
        "apply": "正在安装更新…",
        "launch": "正在启动…",
    }

    def paint(event) -> None:
        p = QPainter(shell)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = shell.width(), shell.height()
        card = p.path = None
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#141826"))
        p.drawRoundedRect(8, 8, w - 16, h - 16, 18, 18)

        cx, cy = w / 2.0, 128.0
        # 唱片底：品牌纸色柔光
        halo = QRadialGradient(cx, cy, 64)
        halo.setColorAt(0.0, QColor(244, 240, 229, 46))
        halo.setColorAt(1.0, QColor(244, 240, 229, 0))
        p.setBrush(halo)
        p.drawEllipse(QRectF(cx - 64, cy - 64, 128, 128))
        p.save()
        p.translate(cx, cy)
        p.rotate(state["spin"])
        pen = p.pen()
        pen.setWidth(4)
        pen.setColor(QColor("#F5513A"))
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(-26, -26, 52, 52, 16 * 30, 16 * 270)
        p.restore()

        p.setPen(QColor("#FCF9F1"))
        p.drawText(
            QRect(24, 208, w - 48, 64),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            PHASE_TEXT.get(state["phase"], PHASE_TEXT["check"]),
        )
        if state["phase"] in ("found", "apply"):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 40))
            p.drawRoundedRect(QRect(64, 262, w - 128, 5), 2, 2)
            if state["progress"] > 0:
                p.setBrush(QColor("#F5513A"))
                p.drawRoundedRect(
                    QRect(64, 262, max(6, int((w - 128) * state["progress"])), 5),
                    2, 2)
        p.end()

    shell.paintEvent = paint
    timer = QTimer(shell)
    timer.timeout.connect(
        lambda: (
            state.__setitem__("spin", (state["spin"] + 4) % 360),
            shell.update(),
        )
    )
    timer.start(33)

    decision = {"action": "launch", "error": None}

    def progress_cb(fraction: float) -> None:
        state["progress"] = max(state["progress"], float(fraction))

    def worker() -> None:
        try:
            plan = decide()
            if plan["action"] == "update":
                state["phase"] = "found"
                perform_update(plan, progress_cb)
                state["phase"] = "apply"
                decision["action"] = "updated"
        except Exception as exc:                     # 降级铁律
            decision["error"] = str(exc)
        finally:
            shell.finished.emit()

    threading.Thread(target=worker, daemon=True).start()
    shell.exec()
    timer.stop()

    relaunch()
    return 0


def run_main() -> int:
    """真正的业务入口（main.py，保持不动的那个）。

    动态导入：既绕过 PyInstaller 静态分析（保证 exe 不含业务代码），
    也让升级后的新业务代码在全新进程中生效。
    """
    ensure_app_on_path()
    os.chdir(app_root())
    main_module = importlib.import_module("main")
    return int(main_module.main())


def main() -> int:
    ensure_app_on_path()
    argv = sys.argv[1:]
    if "--launch" in argv:              # 升级重启后的调用：直接进主程序
        return run_main()
    if "--check" in argv:               # 命令行自检
        return check_cli()
    try:
        return run_boot_shell()
    except SystemExit:
        raise
    except Exception:
        # 外壳自身崩溃（如 PySide6 异常）：保底拉起新进程进主程序
        try:
            relaunch()
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
