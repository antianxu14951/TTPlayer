"""Run LX Music custom-source scripts in a Node sandbox.

A "source" in LX Music is a single JavaScript file. The player hands the script
a ``lx`` object and the script answers two events:

* ``lx.send(lx.EVENT_NAMES.inited, {...})`` announces the platforms it supports
  and the qualities it can resolve.
* ``lx.on(lx.EVENT_NAMES.request, handler)`` turns a song that our own search
  already found into a playable URL.

Node is used rather than an embedded interpreter because the scripts rely on
``setTimeout``, ``Buffer``, ``fetch``, ``atob``/``btoa`` and ``zlib``; Node has
all of them, so even obfuscated scripts run unmodified.

Messages arrive on stdout as JSON lines from a reader thread, which dispatches
them immediately - awaiting callers wait on per-request events.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

def _locate_host_script() -> Path:
    """Resolve ``lx_host.js`` in source runs and in a frozen (onedir) build.

    In a onedir bundle ``__file__`` points inside ``_internal``, but the exact
    layout differs between PyInstaller versions, so the known locations are
    probed instead of trusting a single one.
    """
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            roots.append(Path(meipass))
        roots.append(Path(sys.executable).resolve().parent)
    parent = Path(__file__).resolve().parent
    roots.extend([parent, parent.parent, Path.cwd()])
    relatives = ("js/lx_host.js", "core/js/lx_host.js", "_internal/core/js/lx_host.js")
    for root in roots:
        for relative in relatives:
            candidate = root / relative
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return parent / "js" / "lx_host.js"


HOST_SCRIPT = _locate_host_script()

PLATFORM_NAMES: dict[str, str] = {
    "kw": "酷我音乐",
    "kg": "酷狗音乐",
    "tx": "QQ 音乐",
    "wy": "网易云音乐",
    "mg": "咪咕音乐",
    "bilibili": "哔哩哔哩",
    "bili": "哔哩哔哩",
    "qishui": "汽水音乐",
    "qs": "汽水音乐",
    "qmkg": "全民K歌",
    "kuaishou": "快手",
    "youtube": "YouTube",
    "ytb": "YouTube",
    "xmly": "喜马拉雅",
    "xm": "喜马拉雅",
    "local": "本地文件",
}

QUALITY_NAMES: dict[str, str] = {
    "128k": "标准 128K",
    "192k": "较高 192K",
    "320k": "高品 320K",
    "flac": "无损 FLAC",
    "flac24bit": "Hi-Res 24bit",
    "hires": "Hi-Res",
    "atmos": "杜比全景声",
    "atmos_plus": "杜比全景声+",
    "master": "母带",
}


class LxRuntimeError(RuntimeError):
    """Node (or the host bridge) is unavailable."""


class LxScriptError(RuntimeError):
    """A script failed to load or refused to answer."""


def platform_name(key: str) -> str:
    return PLATFORM_NAMES.get(key, key)


def quality_name(key: str) -> str:
    return QUALITY_NAMES.get(key, key)


def _candidate_paths() -> list[Path]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        roots.append(Path(getattr(sys, "_MEIPASS", "") or "."))
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parent.parent)
    roots.append(Path.cwd())

    names = ("node.exe", "node") if os.name == "nt" else ("node",)
    paths: list[Path] = []
    for root in roots:
        for name in names:
            paths.append(root / "runtime" / name)
            paths.append(root / name)
    return paths


def find_node() -> str | None:
    """Locate Node: env override, bundled copy next to the app, then PATH."""
    override = os.environ.get("MUSICPLAYER_NODE", "").strip()
    if override and Path(override).exists():
        return override

    for path in _candidate_paths():
        try:
            if path.is_file():
                return str(path)
        except OSError:
            continue

    found = shutil.which("node")
    if found:
        return found

    if os.name == "nt":
        bases = [
            Path(os.environ.get("ProgramFiles", r"C:/Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:/Program Files (x86)")),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
        ]
        for base in bases:
            candidate = base / "nodejs" / "node.exe"
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    return None


@dataclass
class LxSourceInfo:
    """One platform offered by a script, as announced in the inited payload."""

    key: str
    name: str
    actions: list[str] = field(default_factory=list)
    qualitys: list[str] = field(default_factory=list)
    has_url: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "actions": list(self.actions),
            "qualitys": list(self.qualitys),
            "has_url": self.has_url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LxSourceInfo":
        return cls(
            key=str(data.get("key") or ""),
            name=str(data.get("name") or ""),
            actions=[str(a) for a in data.get("actions") or []],
            qualitys=[str(q) for q in data.get("qualitys") or []],
            has_url=bool(data.get("has_url", True)),
        )


@dataclass
class LxInited:
    status: bool
    sources: dict[str, LxSourceInfo]
    meta: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def platforms(self) -> list[str]:
        return list(self.sources)

    def describe_platforms(self) -> str:
        if not self.sources:
            return "未声明平台"
        return "、".join(info.name for info in self.sources.values())


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def normalize_sources(raw: Any) -> dict[str, LxSourceInfo]:
    """Accept both the v1 and v2 ``sources`` layouts.

    v1: ``{kw: {name, actions: ['musicUrl'], qualitys: ['128k']}}``
    v2: ``{kw: {name, actions: [{action: 'musicUrl', qualitys: [...]}]}}``
    """
    result: dict[str, LxSourceInfo] = {}
    if not isinstance(raw, dict):
        return result

    for key, item in raw.items():
        if not isinstance(item, dict):
            continue
        actions_raw = item.get("actions")
        actions: list[str] = []
        qualitys: list[str] = []

        if isinstance(actions_raw, list):
            for action in actions_raw:
                if isinstance(action, str):
                    actions.append(action)
                elif isinstance(action, dict) and action.get("action"):
                    actions.append(str(action["action"]))
                    qualitys.extend(str(q) for q in action.get("qualitys") or [])
        elif isinstance(actions_raw, str):
            actions.append(actions_raw)

        if not actions:
            actions = ["musicUrl"]
        if not qualitys:
            qualitys = [str(q) for q in item.get("qualitys") or []]

        result[str(key)] = LxSourceInfo(
            key=str(key),
            name=str(item.get("name") or platform_name(str(key))),
            actions=_dedupe(actions),
            qualitys=_dedupe(qualitys),
            has_url="musicUrl" in actions,
        )
    return result


class LxProcess:
    """One Node process running one source script."""

    def __init__(self, node: str | None = None, *, startup_timeout: float = 15.0) -> None:
        self.node = node or find_node()
        self.startup_timeout = startup_timeout
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()

        self._pending: dict[int, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._next_id = 1

        self._inited_event = threading.Event()
        self._inited: dict[str, Any] | None = None
        self._fatal: str | None = None
        self._exit_event = threading.Event()

        self.meta: dict[str, str] = {}
        self.logs: list[str] = []
        self._closed = False

    # ------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        if not self.node:
            raise LxRuntimeError("未找到 Node.js 运行时，无法加载音源脚本")
        if not HOST_SCRIPT.is_file():
            raise LxRuntimeError(f"缺少宿主脚本：{HOST_SCRIPT}")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        env = dict(os.environ)
        env.setdefault("NODE_OPTIONS", "--no-warnings")

        try:
            self._proc = subprocess.Popen(
                [self.node, str(HOST_SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                env=env,
                cwd=str(HOST_SCRIPT.parent),
            )
        except OSError as exc:
            raise LxRuntimeError(f"无法启动 Node：{exc}") from exc

        self._exit_event.clear()
        self._reader = threading.Thread(target=self._pump, name="lx-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._closed = True
        proc, self._proc = self._proc, None
        reader, self._reader = self._reader, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        # Let the reader drain stdout to EOF so it closes the pipe handle.
        # Without this, every closed source leaks a file descriptor.
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2)
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        self._fail_pending("音源进程已关闭")
        self._exit_event.set()

    def __enter__(self) -> "LxProcess":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------ plumbing

    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            self._handle(message)

        # stdout closed: nothing else will arrive. Close our end of the pipe
        # as well so a script that exits on its own leaves no open descriptor.
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        self._fail_pending("音源进程已退出")
        self._exit_event.set()
        self._inited_event.set()

    def _fail_pending(self, reason: str) -> None:
        with self._pending_lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot["message"] = {"t": "error", "message": reason}
            slot["event"].set()

    def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("t")
        if kind == "inited":
            self._inited = message.get("data") or {}
            self._inited_event.set()
        elif kind == "meta":
            self.meta = {k: str(v) for k, v in (message.get("data") or {}).items() if v}
        elif kind == "log":
            self.logs.append(str(message.get("msg") or ""))
            if len(self.logs) > 200:
                del self.logs[:100]
        elif kind == "fatal":
            self._fatal = str(message.get("message") or "脚本异常")
            self._inited_event.set()
        elif kind in ("result", "error"):
            with self._pending_lock:
                slot = self._pending.pop(message.get("id"), None)
            if slot is not None:
                slot["message"] = message
                slot["event"].set()

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise LxScriptError("音源进程不可用")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise LxScriptError("音源进程已退出") from exc

    # ------------------------------------------------------------ public API

    def load(self, code: str, *, source_url: str = "") -> LxInited:
        """Execute the script and wait for its ``inited`` announcement."""
        self.start()
        self._inited = None
        self._fatal = None
        self.meta = {}
        self._inited_event.clear()

        self._send({"cmd": "load", "code": code, "info": {"sourceUrl": source_url}})

        finished = self._inited_event.wait(self.startup_timeout)
        if self._fatal:
            raise LxScriptError(self._fatal)
        if not finished or self._inited is None:
            if self._exit_event.is_set():
                raise LxScriptError("音源进程意外退出")
            detail = self.logs[-1].strip() if self.logs else ""
            hint = f"：{detail}" if detail else "（接口不可达或脚本不兼容）"
            raise LxScriptError("脚本未返回初始化信息" + hint)

        payload = self._inited
        status = bool(payload.get("status", True))
        if not status:
            raise LxScriptError(str(payload.get("message") or "脚本初始化失败"))
        sources = normalize_sources(payload.get("sources"))
        return LxInited(status=status, sources=sources, meta=dict(self.meta), raw=payload)

    def call(
        self,
        source: str,
        action: str,
        info: dict[str, Any],
        *,
        timeout: float = 25.0,
    ) -> Any:
        """Invoke the script's request handler and return its resolved value."""
        if not self.running:
            raise LxScriptError("音源进程未运行")

        with self._pending_lock:
            call_id = self._next_id
            self._next_id += 1
            slot: dict[str, Any] = {"event": threading.Event(), "message": None}
            self._pending[call_id] = slot

        self._send(
            {"cmd": "call", "id": call_id, "source": source, "action": action, "info": info}
        )

        if not slot["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(call_id, None)
            raise LxScriptError("解析超时，请检查网络或更换音源")

        message = slot["message"] or {}
        if message.get("t") == "error":
            raise LxScriptError(str(message.get("message") or "脚本返回错误"))
        return message.get("data")


def read_script_meta(code: str) -> dict[str, str]:
    """Parse the ``/*! @name ... */`` banner that LX scripts start with."""
    import re

    head = code[:4000]
    banner = re.search(r"/\*[\s\S]{0,3000}?\*/", head)
    block = banner.group(0) if banner else head[:2000]

    def field(name: str) -> str:
        hit = re.search(rf"@{name}\s+([^\n\r*]+)", block)
        return hit.group(1).strip() if hit else ""

    return {
        "name": field("name"),
        "description": field("description"),
        "version": field("version"),
        "author": field("author"),
        "homepage": field("homepage"),
    }


def probe_script(code: str, *, source_url: str = "", timeout: float = 20.0) -> LxInited:
    """Load a script once and report its metadata and platforms."""
    process = LxProcess(startup_timeout=timeout)
    try:
        result = process.load(code, source_url=source_url)
        if not result.meta.get("name"):
            result.meta = {**read_script_meta(code), **result.meta}
        return result
    finally:
        process.close()
