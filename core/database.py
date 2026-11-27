"""SQLite persistence for settings, tracks and playlists.

The database is the single source of truth at rest. On startup the library is
loaded into memory once, so all UI reads stay O(1) while writes are grouped
into short transactions. Existing JSON data is migrated in place on first run.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from core.models import MusicSource, Playlist, Track

SCHEMA_VERSION = 3

_PLAYBACK_FIELDS = (
    "playlist_id",
    "track_id",
    "position_ms",
    "volume",
    "audio_device_id",
)

# Schema is created incrementally at runtime, never shipped as a database file.
# Each table/index is created only when missing, and columns introduced in
# later versions are added in place. Existing rows are never deleted or
# rewritten, so upgrading keeps whatever data the user has accumulated.
_TABLE_DDL: dict[str, str] = {
    "settings": """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """,
    "tracks": """
        CREATE TABLE IF NOT EXISTS tracks (
            id          TEXT PRIMARY KEY,
            path        TEXT NOT NULL,
            title       TEXT NOT NULL,
            artist      TEXT NOT NULL DEFAULT '',
            album       TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            cover_file  TEXT NOT NULL DEFAULT '',
            source      TEXT NOT NULL DEFAULT 'local',
            source_url  TEXT NOT NULL DEFAULT '',
            added_at    TEXT NOT NULL DEFAULT ''
        )
    """,
    "playlists": """
        CREATE TABLE IF NOT EXISTS playlists (
            id         TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT ''
        )
    """,
    "playlist_tracks": """
        CREATE TABLE IF NOT EXISTS playlist_tracks (
            playlist_id TEXT NOT NULL,
            track_id    TEXT NOT NULL,
            position    INTEGER NOT NULL,
            PRIMARY KEY (playlist_id, track_id),
            FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        )
    """,
    # One row holds everything needed to restore the last listening session.
    "playback_state": """
        CREATE TABLE IF NOT EXISTS playback_state (
            id              INTEGER PRIMARY KEY CHECK (id = 1),
            playlist_id     TEXT NOT NULL DEFAULT '',
            track_id        TEXT NOT NULL DEFAULT '',
            position_ms     INTEGER NOT NULL DEFAULT 0,
            volume          REAL NOT NULL DEFAULT 0.7,
            audio_device_id TEXT NOT NULL DEFAULT '',
            updated_at      TEXT NOT NULL DEFAULT ''
        )
    """,
    # Each row is a full MusicSource serialized as JSON, so new protocol fields
    # can be added without another schema migration.
    "music_sources": """
        CREATE TABLE IF NOT EXISTS music_sources (
            id      TEXT PRIMARY KEY,
            name    TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            payload TEXT NOT NULL DEFAULT '{}'
        )
    """,
}

_INDEX_DDL: dict[str, str] = {
    "idx_tracks_path": "CREATE INDEX IF NOT EXISTS idx_tracks_path ON tracks(path)",
    "idx_playlist_tracks_track": (
        "CREATE INDEX IF NOT EXISTS idx_playlist_tracks_track "
        "ON playlist_tracks(track_id)"
    ),
}

# Columns that may be absent in a database created by an older build. Keys are
# the column definitions used by ALTER TABLE; primary-key columns are omitted
# because SQLite cannot add them after the fact.
_ADD_COLUMNS: dict[str, dict[str, str]] = {
    "tracks": {
        "artist": "TEXT NOT NULL DEFAULT ''",
        "album": "TEXT NOT NULL DEFAULT ''",
        "duration_ms": "INTEGER NOT NULL DEFAULT 0",
        "cover_file": "TEXT NOT NULL DEFAULT ''",
        "source": "TEXT NOT NULL DEFAULT 'local'",
        "source_url": "TEXT NOT NULL DEFAULT ''",
        "added_at": "TEXT NOT NULL DEFAULT ''",
    },
    "playlists": {"created_at": "TEXT NOT NULL DEFAULT ''"},
    "playlist_tracks": {"position": "INTEGER NOT NULL DEFAULT 0"},
    "playback_state": {
        "playlist_id": "TEXT NOT NULL DEFAULT ''",
        "track_id": "TEXT NOT NULL DEFAULT ''",
        "position_ms": "INTEGER NOT NULL DEFAULT 0",
        "volume": "REAL NOT NULL DEFAULT 0.7",
        "audio_device_id": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    },
    "music_sources": {
        "name": "TEXT NOT NULL DEFAULT ''",
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "payload": "TEXT NOT NULL DEFAULT '{}'",
    },
}


def ensure_table(connection: Any, name: str, ddl: str) -> bool:
    """Create ``name`` only when missing; return whether it was created."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    if exists is not None:
        return False
    connection.execute(ddl)
    return True


class Database:
    """Thin, synchronous SQLite wrapper used by settings and Library."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self.ensure_schema()

    def ensure_schema(self) -> list[str]:
        """Add missing tables/indexes/columns without touching existing data.

        Called on every startup (and on every redirected connection). A user
        database is therefore upgraded in place: rows are preserved and only
        the pieces the current build needs are created.
        """
        connection = self._connection
        existing_objects = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
        changes: list[str] = []

        for name, ddl in _TABLE_DDL.items():
            if name not in existing_objects:
                connection.execute(ddl)
                changes.append(f"表 {name}")

        for name, ddl in _INDEX_DDL.items():
            if name not in existing_objects:
                connection.execute(ddl)
                changes.append(f"索引 {name}")

        for table, columns in _ADD_COLUMNS.items():
            present = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for column, definition in columns.items():
                if column not in present:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )
                    changes.append(f"字段 {table}.{column}")

        if changes:
            connection.commit()
        return changes

    def close(self) -> None:
        self._connection.close()

    # ---------- settings ----------

    def all_settings(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for row in self._connection.execute("SELECT key, value FROM settings"):
            result[str(row["key"])] = _decode(row["value"])
        return result

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._connection.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return _decode(row["value"]) if row is not None else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def set_settings(self, values: dict[str, Any]) -> None:
        with self._connection:
            self._connection.executemany(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [
                    (key, json.dumps(value, ensure_ascii=False))
                    for key, value in values.items()
                ],
            )

    def delete_setting(self, key: str) -> None:
        """删除单个设置键（用于清理已废弃的配置项）。"""
        with self._connection:
            self._connection.execute("DELETE FROM settings WHERE key = ?", (key,))

    # ---------- playback state ----------

    def load_playback_state(self) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT playlist_id, track_id, position_ms, volume, audio_device_id "
            "FROM playback_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return {}
        return {
            "playlist_id": str(row["playlist_id"] or ""),
            "track_id": str(row["track_id"] or ""),
            "position_ms": int(row["position_ms"] or 0),
            "volume": float(row["volume"]),
            "audio_device_id": str(row["audio_device_id"] or ""),
        }

    def save_playback_state(self, values: dict[str, Any]) -> None:
        """Persist the latest listening session in a single upsert."""
        current = self.load_playback_state()
        current.update(
            {key: value for key, value in values.items() if key in _PLAYBACK_FIELDS}
        )
        current["volume"] = float(current.get("volume", 0.7))
        current["position_ms"] = int(current.get("position_ms", 0))
        with self._connection:
            self._connection.execute(
                "INSERT INTO playback_state(id, playlist_id, track_id, position_ms, "
                "volume, audio_device_id, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "playlist_id = excluded.playlist_id, track_id = excluded.track_id, "
                "position_ms = excluded.position_ms, volume = excluded.volume, "
                "audio_device_id = excluded.audio_device_id, updated_at = excluded.updated_at",
                (
                    current.get("playlist_id", ""),
                    current.get("track_id", ""),
                    current["position_ms"],
                    current["volume"],
                    current.get("audio_device_id", ""),
                    time.time(),
                ),
            )

    def forget_playback_refs(
        self, playlist_id: str, track_ids: Iterable[str]
    ) -> None:
        """Blank out saved-session fields that point at deleted data.

        Deleting a playlist removes its rows; a stale playlist_id or track_id
        in playback_state would otherwise survive until the next save.
        """
        ids = set(track_ids)
        state = self.load_playback_state()
        if not state:
            return
        stale_playlist = state.get("playlist_id") == playlist_id
        stale_track = state.get("track_id") in ids
        if not (stale_playlist or stale_track):
            return
        if stale_playlist:
            state["playlist_id"] = ""
        if stale_track:
            state["track_id"] = ""
            state["position_ms"] = 0
        self.save_playback_state(state)

    # ---------- music sources ----------

    def load_sources(self) -> list[MusicSource]:
        sources: list[MusicSource] = []
        for row in self._connection.execute(
            "SELECT payload FROM music_sources ORDER BY rowid"
        ):
            try:
                source = MusicSource.from_dict(_decode(str(row["payload"])))
            except (TypeError, ValueError):
                continue
            if source.id:
                sources.append(source)
        return sources

    def replace_sources(self, sources: Iterable[MusicSource]) -> None:
        """Persist the full source list in one transaction, preserving order."""
        with self._connection:
            self._connection.execute("DELETE FROM music_sources")
            self._connection.executemany(
                "INSERT INTO music_sources(id, name, enabled, payload) VALUES (?, ?, ?, ?)",
                [
                    (
                        source.id,
                        source.name,
                        1 if source.enabled else 0,
                        json.dumps(source.to_dict(), ensure_ascii=False),
                    )
                    for source in sources
                ],
            )

    # ---------- library ----------

    def load_library(self) -> tuple[dict[str, Track], dict[str, Playlist]]:
        tracks: dict[str, Track] = {}
        for row in self._connection.execute("SELECT * FROM tracks"):
            track = Track.from_dict(dict(row))
            if track.id and track.path:
                tracks[track.id] = track

        playlists: dict[str, Playlist] = {}
        for row in self._connection.execute("SELECT * FROM playlists ORDER BY rowid"):
            playlist = Playlist(
                id=str(row["id"]),
                name=str(row["name"]),
                created_at=str(row["created_at"] or ""),
            )
            playlists[playlist.id] = playlist

        members: dict[str, list[str]] = {}
        for row in self._connection.execute(
            "SELECT playlist_id, track_id FROM playlist_tracks "
            "ORDER BY playlist_id, position"
        ):
            members.setdefault(str(row["playlist_id"]), []).append(str(row["track_id"]))
        for playlist_id, track_ids in members.items():
            if playlist_id in playlists:
                playlists[playlist_id].track_ids = [
                    track_id for track_id in track_ids if track_id in tracks
                ]
        return tracks, playlists

    def replace_library(
        self, tracks: Iterable[Track], playlists: Iterable[Playlist]
    ) -> None:
        """Atomically persist the full in-memory library in one transaction."""
        track_list = list(tracks)
        playlist_list = list(playlists)
        with self._connection:
            self._connection.execute("DELETE FROM playlist_tracks")
            self._connection.execute("DELETE FROM playlists")
            self._connection.execute("DELETE FROM tracks")
            self._connection.executemany(
                "INSERT INTO tracks(id, path, title, artist, album, duration_ms, "
                "cover_file, source, source_url, added_at) "
                "VALUES (:id, :path, :title, :artist, :album, :duration_ms, "
                ":cover_file, :source, :source_url, :added_at)",
                [track.to_dict() for track in track_list],
            )
            self._connection.executemany(
                "INSERT INTO playlists(id, name, created_at) VALUES (?, ?, ?)",
                [
                    (playlist.id, playlist.name, playlist.created_at)
                    for playlist in playlist_list
                ],
            )
            members = []
            for playlist in playlist_list:
                for position, track_id in enumerate(playlist.track_ids):
                    members.append((playlist.id, track_id, position))
            self._connection.executemany(
                "INSERT INTO playlist_tracks(playlist_id, track_id, position) "
                "VALUES (?, ?, ?)",
                members,
            )


def _decode(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def migrate_legacy(
    db: Database, library_file: Path, config_file: Path
) -> list[str]:
    """Import legacy JSON files once, leaving renamed backups behind.

    Returns a short list of human-readable descriptions of what was migrated.
    Safe to call on every startup: each source is only consumed when the
    corresponding SQLite table is still empty.
    """
    notes: list[str] = []

    if config_file.is_file() and not db.all_settings():
        try:
            with config_file.open("r", encoding="utf-8") as handle:
                stored = json.load(handle)
            if isinstance(stored, dict) and stored:
                db.set_settings(stored)
                _retire_file(config_file)
                notes.append(f"配置（{len(stored)} 项）")
        except (OSError, ValueError):
            pass

    tracks, playlists = db.load_library()
    if library_file.is_file() and not tracks and not playlists:
        try:
            with library_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            imported_tracks = [
                Track.from_dict(item)
                for item in payload.get("tracks", [])
                if isinstance(item, dict)
            ]
            imported_playlists = [
                Playlist.from_dict(item)
                for item in payload.get("playlists", [])
                if isinstance(item, dict)
            ]
            db.replace_library(imported_tracks, imported_playlists)
            _retire_file(library_file)
            notes.append(
                f"媒体库（{len(imported_tracks)} 首曲目、{len(imported_playlists)} 个歌单）"
            )
        except (OSError, ValueError, TypeError):
            pass
    return notes


def _retire_file(path: Path) -> None:
    backup = path.with_name(path.name + ".migrated")
    if backup.exists():
        backup = path.with_name(f"{path.name}.migrated.{int(time.time())}")
    try:
        path.replace(backup)
    except OSError:
        pass  # Keep the original file if it cannot be renamed.
