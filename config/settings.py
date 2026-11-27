from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

APP_NAME = "MusicPlayer"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _app_root() -> Path:
    """软件根目录：源码运行时为项目根目录，打包后为 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


APP_ROOT = _app_root()
# 默认缓存位置；用户可在「设置」中修改，实际生效值由 get_cache_dir() 解析。
CACHE_DIR = APP_ROOT / ".cache"


def _default_data_dir() -> Path:
    """Resolve the data directory from environment/platform conventions.

    No user-specific absolute path is embedded in the source code. Setting
    MUSICPLAYER_DATA_DIR overrides the location for portable installations.
    """
    override = os.environ.get("MUSICPLAYER_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


DATA_DIR = _default_data_dir()
MEDIA_DIR = DATA_DIR / "media"
TEMP_DIR = DATA_DIR / "temp"
COVER_DIR = DATA_DIR / "covers"
DB_FILE = DATA_DIR / "musicplayer.db"
# The pointer lives outside SQLite on purpose: settings are stored inside the
# database, so the location of the database must be readable before it opens.
DB_LOCATION_FILE = DATA_DIR / "database_location.json"
# Legacy locations, read once and migrated into SQLite on first run.
LIBRARY_FILE = DATA_DIR / "library.json"
CONFIG_FILE = DATA_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "cache_dir": str(CACHE_DIR),
    "ffmpeg_path": shutil.which("ffmpeg") or "ffmpeg",
    "request_timeout_seconds": 45,
    "download_timeout_seconds": 120,
    "douyin_download_retries": 3,
    "douyin_download_retry_delay_seconds": 2.0,
    "douyin_quality": "1080p",
    "douyin_qualities": ["1080p", "720p", "540p"],
    "douyin_fetch_retries": 3,
    "douyin_retry_delay_seconds": 2.0,
    "audio_extensions": [".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma", ".opus"],
    "volume": 0.7,
    "playback_mode": "list_loop",
    "theme": "daylight",
    "background_image": "",
}

_db: Any = None
_db_path: Path | None = None
_db_file_override: Path | None = None


def _read_db_location() -> Path | None:
    """Read the location pointer; ignore a missing or damaged file."""
    try:
        payload = json.loads(DB_LOCATION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = payload.get("db_file") if isinstance(payload, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def reload_db_location() -> Path:
    """Re-read the pointer (mainly for tests and external edits)."""
    global _db_file_override
    _db_file_override = _read_db_location()
    return current_db_file()


_db_file_override = _read_db_location()


def current_db_file() -> Path:
    """Return the SQLite file this process should use."""
    if _db_file_override is not None:
        return _db_file_override
    return Path(DB_FILE).expanduser().resolve()


def _write_db_location(path: Path) -> None:
    global _db_file_override
    atomic_write_json(DB_LOCATION_FILE, {"db_file": str(path)})
    _db_file_override = path.resolve()


def switch_database_file(
    target: Path | str, before_switch: Callable[[], None] | None = None
) -> str | None:
    """Move the live database to ``target`` and remember it for next launch.

    The move is deliberately conservative: an existing destination is never
    overwritten, and a failure restores both the files and the old pointer.
    ``before_switch`` lets the caller close extra SQLite connections (for
    example the Library's own handle) before Windows tries to move the file.
    Returns an error message, or ``None`` on success.
    """
    global _db, _db_path, _db_file_override
    try:
        destination = Path(target).expanduser().resolve()
    except OSError:
        return "路径无法解析，请检查输入"
    source = current_db_file()
    if destination == source:
        return None
    if destination.exists():
        if destination.is_dir():
            return "目标路径是一个文件夹，请填写完整的数据库文件名"
        return "目标文件已存在，为避免覆盖已有数据，请选择其他位置"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"无法创建目标目录：{exc}"

    had_override = _db_file_override is not None
    close_db()
    if before_switch is not None:
        try:
            before_switch()
        except Exception as exc:  # noqa: BLE001 - report to the UI
            return f"无法关闭现有数据库连接：{exc}"

    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in ("", "-wal", "-shm"):
            old = Path(str(source) + suffix)
            if old.exists():
                new = Path(str(destination) + suffix)
                shutil.move(str(old), str(new))
                moved.append((new, old))
        _write_db_location(destination)
        get_db()  # Fail here, while rollback is still possible.
        return None
    except (OSError, sqlite3.Error, ValueError) as exc:
        for new, old in reversed(moved):
            try:
                shutil.move(str(new), str(old))
            except OSError:
                pass
        try:
            if had_override:
                _write_db_location(source)
            else:
                DB_LOCATION_FILE.unlink(missing_ok=True)
                _db_file_override = None
        except OSError:
            pass
        return f"数据库迁移失败：{exc}"


def get_db():  # type: ignore[no-untyped-def]
    """Return the shared database, recreated if the path is redirected."""
    global _db, _db_path
    target = current_db_file()
    if _db is None or _db_path != target:
        from core.database import Database, migrate_legacy

        if _db is not None:
            try:
                _db.close()
            except sqlite3.Error:
                pass
        database = Database(target)
        migrate_legacy(database, LIBRARY_FILE, CONFIG_FILE)
        _db = database
        _db_path = target
    return _db


def close_db() -> None:
    global _db, _db_path
    if _db is not None:
        _db.close()
    _db = None
    _db_path = None


def ensure_dirs() -> None:
    for directory in (DATA_DIR, MEDIA_DIR, TEMP_DIR, COVER_DIR, get_cache_dir()):
        directory.mkdir(parents=True, exist_ok=True)


def get_cache_dir() -> Path:
    """Resolve the user-configured cache directory for Douyin downloads."""
    configured = str(load_config().get("cache_dir") or "").strip()
    if not configured:
        return CACHE_DIR
    try:
        return Path(configured).expanduser().resolve()
    except OSError:
        return CACHE_DIR


def atomic_write_json(path: Path, payload: Any) -> None:
    """Kept for tools that may still export a JSON snapshot; not used by Library."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# 已废弃的配置键：功能删除后从 DEFAULT_CONFIG 移除，但旧用户数据库里可能
# 仍有残留行——load/save 时统一剔除并删除，避免被原样写回。
_REMOVED_KEYS = ("background_opacity", "ripple_effect")


def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    try:
        config.update(get_db().all_settings())
    except (OSError, ValueError):
        pass
    for key in _REMOVED_KEYS:
        config.pop(key, None)
    return config


def save_config(config: dict[str, Any]) -> None:
    # 与 load_config 对应：废弃键不再持久化；数据库里已有的旧行顺带删除。
    for key in _REMOVED_KEYS:
        config.pop(key, None)
    get_db().set_settings(config)
    try:
        for key in _REMOVED_KEYS:
            get_db().delete_setting(key)
    except Exception:  # noqa: BLE001 - 清理失败不影响主流程
        pass


def load_playback_state() -> dict[str, Any]:
    """Return the persisted last-listening session (empty dict on first run)."""
    try:
        return get_db().load_playback_state()
    except (OSError, ValueError):
        return {}


def save_playback_state(values: dict[str, Any]) -> None:
    """Persist selected playback fields without losing the others."""
    get_db().save_playback_state(values)


def load_sources() -> list[Any]:  # type: ignore[no-untyped-def]
    """Return all configured aggregated-search sources (empty on first run)."""
    try:
        return get_db().load_sources()
    except (OSError, ValueError):
        return []


def save_sources(sources: list[Any]) -> None:  # type: ignore[no-untyped-def]
    """Atomically persist the full aggregated-search source list."""
    get_db().replace_sources(sources)
