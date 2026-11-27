"""LX Music custom sources: storage, capability model and URL resolution.

A source is a single JavaScript file. The user supplies one URL (or picks a
local ``.js``); everything else - display name, version, supported platforms
and qualities - is read from the script itself, first from its ``/*! @name */``
banner and then from the ``inited`` payload it sends when executed.

The script does not search. It only turns a song that our own search already
found into a playable URL, which is exactly the LX contract:

    handler({ source, action: 'musicUrl', info: { type, musicInfo } })

``LxResolver`` keeps one Node process per source and tries sources in order
until one returns a URL.
"""
from __future__ import annotations

import ast
import html
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

from config.settings import get_db
from core.lx_proc import (
    LxInited,
    LxProcess,
    LxRuntimeError,
    LxScriptError,
    LxSourceInfo,
    find_node,
    platform_name,
    probe_script,
    quality_name,
    read_script_meta,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Low to high. Used to negotiate a quality the source can actually deliver.
QUALITY_ORDER: tuple[str, ...] = (
    "128k",
    "192k",
    "320k",
    "flac",
    "flac24bit",
    "hires",
    "atmos",
    "atmos_plus",
    "master",
)

# What our own search can look up, independent of any script.
SEARCHABLE_PLATFORMS: tuple[str, ...] = ("kw", "kg", "tx", "wy")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lx_sources (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    version     TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    script_url  TEXT NOT NULL DEFAULT '',
    script      TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    platforms   TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL DEFAULT ''
);
"""


def normalize_version(value: str) -> str:
    """Scripts write ``@version v3.0.1`` or ``3.0.1``; display one way."""
    text = (value or "").strip()
    return text[1:] if text[:1] in ("v", "V") else text


def quality_rank(quality: str) -> int:
    try:
        return QUALITY_ORDER.index(quality)
    except ValueError:
        return -1


def best_quality(available: Iterable[str], wanted: str = "") -> str:
    """Pick the closest quality at or below ``wanted``, else the best offered."""
    offered = [q for q in available if q]
    if not offered:
        return "128k"
    if wanted and wanted in offered:
        return wanted
    wanted_rank = quality_rank(wanted) if wanted else len(QUALITY_ORDER) - 1
    lower = [q for q in offered if quality_rank(q) <= wanted_rank]
    if lower:
        return max(lower, key=quality_rank)
    return min(offered, key=quality_rank) if len(offered) > 1 else offered[0]


@dataclass
class LxSource:
    """One installed script plus the capabilities it announced."""

    id: str
    name: str
    version: str = ""
    author: str = ""
    description: str = ""
    script_url: str = ""
    script: str = ""
    enabled: bool = True
    platforms: dict[str, LxSourceInfo] = field(default_factory=dict)
    updated_at: str = ""

    # ---------------------------------------------------------- capabilities

    def supports(self, platform: str) -> bool:
        info = self.platforms.get(platform)
        return bool(info and info.has_url)

    def qualitys_for(self, platform: str) -> list[str]:
        info = self.platforms.get(platform)
        return list(info.qualitys) if info else []

    def platform_keys(self) -> list[str]:
        return [key for key in self.platforms]

    def platform_label(self) -> str:
        if not self.platforms:
            return "未声明平台"
        return "、".join(info.name for info in self.platforms.values())

    def searchable_platforms(self) -> list[str]:
        return [key for key in self.platforms if key in SEARCHABLE_PLATFORMS and self.supports(key)]

    # ---------------------------------------------------------- serialisation

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "script_url": self.script_url,
            "enabled": bool(self.enabled),
            "platforms": {key: info.to_dict() for key, info in self.platforms.items()},
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: Any) -> "LxSource":
        try:
            raw_platforms = json.loads(row["platforms"] or "{}")
        except ValueError:
            raw_platforms = {}
        platforms = {
            str(key): LxSourceInfo.from_dict(value)
            for key, value in (raw_platforms or {}).items()
            if isinstance(value, dict)
        }
        return cls(
            id=str(row["id"]),
            name=str(row["name"] or ""),
            version=str(row["version"] or ""),
            author=str(row["author"] or ""),
            description=str(row["description"] or ""),
            script_url=str(row["script_url"] or ""),
            script=str(row["script"] or ""),
            enabled=bool(row["enabled"]),
            platforms=platforms,
            updated_at=str(row["updated_at"] or ""),
        )


# ---------------------------------------------------------------- store


class LxStoreError(RuntimeError):
    """The source table could not be read or written."""


class LxSourceStore:
    """SQLite persistence for installed scripts."""

    def __init__(self) -> None:
        # The connection the schema was last prepared on. Tests redirect the
        # settings paths, so ``get_db`` can hand back a fresh Database at any
        # time; the schema must follow the connection, not the first visit.
        self._prepared_for: Any = None
        self._lock = threading.Lock()

    def _conn(self) -> Any:
        import sqlite3

        conn = get_db()._connection  # noqa: SLF001 - shared sqlite handle
        with self._lock:
            if self._prepared_for is not conn:
                try:
                    from core.database import ensure_table

                    if ensure_table(conn, "lx_sources", _SCHEMA):
                        conn.commit()
                except sqlite3.OperationalError as exc:
                    raise LxStoreError(
                        "音源数据库当前不可写（可能被其他程序占用），请关闭后重试"
                    ) from exc
                self._prepared_for = conn
        return conn

    def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        import sqlite3

        conn = self._conn()
        try:
            with conn:
                conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            raise LxStoreError(f"保存音源失败：{exc}") from exc

    def load(self) -> list[LxSource]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM lx_sources ORDER BY rowid").fetchall()
        return [LxSource.from_row(row) for row in rows]

    def upsert(self, source: LxSource) -> None:
        import sqlite3

        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO lx_sources
                        (id, name, version, author, description, script_url, script,
                         enabled, platforms, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name, version=excluded.version,
                        author=excluded.author, description=excluded.description,
                        script_url=excluded.script_url, script=excluded.script,
                        enabled=excluded.enabled, platforms=excluded.platforms,
                        updated_at=excluded.updated_at
                    """,
                    (
                        source.id,
                        source.name,
                        source.version,
                        source.author,
                        source.description,
                        source.script_url,
                        source.script,
                        1 if source.enabled else 0,
                        json.dumps(
                            {k: v.to_dict() for k, v in source.platforms.items()},
                            ensure_ascii=False,
                        ),
                        source.updated_at,
                    ),
                )
        except sqlite3.OperationalError as exc:
            raise LxStoreError(f"保存音源失败：{exc}") from exc

    def delete(self, source_id: str) -> None:
        import sqlite3

        conn = self._conn()
        try:
            with conn:
                conn.execute("DELETE FROM lx_sources WHERE id = ?", (source_id,))
        except sqlite3.OperationalError as exc:
            raise LxStoreError(f"删除音源失败：{exc}") from exc

    def set_enabled(self, source_id: str, enabled: bool) -> None:
        import sqlite3

        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE lx_sources SET enabled = ? WHERE id = ?",
                    (1 if enabled else 0, source_id),
                )
        except sqlite3.OperationalError as exc:
            raise LxStoreError(f"更新音源状态失败：{exc}") from exc


# ---------------------------------------------------------------- loading


def is_remote(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def fetch_script(url: str, timeout: float = 20.0) -> str:
    """Read a script from a URL or from disk."""
    if is_remote(url):
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise LxScriptError(f"下载音源脚本失败：{exc}") from exc
        text = response.text
        if "<html" in text[:400].lower():
            raise LxScriptError("该地址返回的是网页而不是脚本，请填写以 .js 结尾的直链")
        return text

    path = Path(url)
    if not path.is_file():
        raise LxScriptError(f"找不到脚本文件：{url}")
    return path.read_text(encoding="utf-8", errors="replace")


def build_source(url: str, *, timeout: float = 20.0, existing_id: str = "") -> LxSource:
    """Load one script and turn it into an installed source.

    This is the whole "add a source" flow: one URL in, a ready source out.
    """
    code = fetch_script(url, timeout=timeout)
    if "globalThis.lx" not in code and "lx.EVENT_NAMES" not in code and "EVENT_NAMES" not in code:
        raise LxScriptError("这不是洛雪（LX Music）音源脚本：未找到 lx 事件协议")

    inited: LxInited = probe_script(code, source_url=url, timeout=max(15.0, timeout))
    meta = inited.meta or read_script_meta(code)

    if not inited.sources:
        raise LxScriptError("脚本没有声明任何音源平台，可能已失效")

    name = (meta.get("name") or "").strip()
    if not name:
        parsed = urlparse(url)
        name = Path(parsed.path).stem if parsed.path else "未命名音源"

    return LxSource(
        id=existing_id or uuid.uuid4().hex[:12],
        name=name,
        version=normalize_version(meta.get("version", "")),
        author=(meta.get("author") or "").strip(),
        description=(meta.get("description") or "").strip(),
        script_url=url,
        script=code,
        enabled=True,
        platforms=inited.sources,
        updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )


# ---------------------------------------------------------------- resolver


class LxResolver:
    """Keeps one sandboxed Node process per source and resolves play URLs."""

    def __init__(self) -> None:
        self._procs: dict[str, LxProcess] = {}
        self._lock = threading.Lock()

    def _process(self, source: LxSource) -> LxProcess:
        with self._lock:
            proc = self._procs.get(source.id)
            if proc is not None and proc.running:
                return proc
            if proc is not None:
                proc.close()
            proc = LxProcess(startup_timeout=20)
            proc.start()
            self._procs[source.id] = proc
        # Load outside the lock: scripts may perform network bootstrap.
        try:
            proc.load(source.script, source_url=source.script_url)
        except LxScriptError:
            self.drop(source.id)
            raise
        return proc

    def drop(self, source_id: str) -> None:
        with self._lock:
            proc = self._procs.pop(source_id, None)
        if proc is not None:
            proc.close()

    def close(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
            self._procs.clear()
        for proc in procs:
            proc.close()

    def resolve_one(
        self,
        source: LxSource,
        platform: str,
        quality: str,
        music_info: dict[str, Any],
        *,
        timeout: float = 25.0,
    ) -> tuple[str, str]:
        """Return ``(url, quality_used)`` for a single source."""
        used_quality = best_quality(source.qualitys_for(platform), quality)
        proc = self._process(source)
        result = proc.call(
            platform,
            "musicUrl",
            {"type": used_quality, "musicInfo": music_info},
            timeout=timeout,
        )
        if isinstance(result, dict):
            result = result.get("url") or ""
        if not isinstance(result, str) or not result.startswith("http"):
            raise LxScriptError(f"{source.name} 未返回有效地址")
        return result, used_quality

    def resolve(
        self,
        sources: Iterable[LxSource],
        platform: str,
        quality: str,
        music_info: dict[str, Any],
        *,
        timeout: float = 25.0,
    ) -> tuple[str, LxSource, str]:
        """Try every enabled source that supports ``platform`` in order."""
        candidates = [s for s in sources if s.enabled and s.supports(platform)]
        if not candidates:
            label = platform_name(platform)
            raise LxScriptError(f"没有已启用的音源支持{label}，请先在「音源管理」中添加")

        errors: list[str] = []
        for source in candidates:
            try:
                url, used = self.resolve_one(
                    source, platform, quality, music_info, timeout=timeout
                )
                return url, source, used
            except (LxScriptError, LxRuntimeError) as exc:
                errors.append(f"{source.name}：{exc}")
                # A crashed process must not be reused.
                self.drop(source.id)
                continue

        detail = "；".join(errors[:3])
        if len(errors) > 3:
            detail += f" 等 {len(errors)} 个音源"
        raise LxScriptError(f"所有音源都解析失败（{detail}）")


_store: LxSourceStore | None = None
_resolver: LxResolver | None = None


def get_store() -> LxSourceStore:
    global _store
    if _store is None:
        _store = LxSourceStore()
    return _store


def get_resolver() -> LxResolver:
    global _resolver
    if _resolver is None:
        _resolver = LxResolver()
    return _resolver


def close_resolver() -> None:
    global _resolver
    if _resolver is not None:
        _resolver.close()
        _resolver = None


def load_sources() -> list[LxSource]:
    return get_store().load()


def enabled_sources() -> list[LxSource]:
    return [source for source in load_sources() if source.enabled]


def describe_enabled() -> str:
    """Short summary for the settings card."""
    sources = enabled_sources()
    if not sources:
        return "未安装音源"
    platforms: list[str] = []
    for source in sources:
        for key in source.platform_keys():
            label = platform_name(key)
            if label not in platforms:
                platforms.append(label)
    return f"{len(sources)} 个音源已启用 · 支持 {'、'.join(platforms)}"
