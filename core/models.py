from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# The bucket that catches everything imported without an explicit target. Its
# id is reserved rather than generated: playlists are the only way into the
# library now, so this one has to be recognisable without a schema flag.
DEFAULT_PLAYLIST_ID = "default"
DEFAULT_PLAYLIST_NAME = "默认歌单"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_str(value: Any, default: str = "") -> str:
    """Treat a persisted ``null`` as absent instead of the literal "None".

    ``load_library`` filters tracks with ``if track.id and track.path``; a
    null path turned into the string "None" would pass that filter and leave
    an unplayable ghost entry in the library.
    """
    return default if value is None else str(value)


def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce a persisted value to int without letting one bad field abort a load.

    ``duration_ms`` arrives from SQLite (declared INTEGER but dynamically
    typed) and from legacy JSON that users can edit by hand. A single corrupt
    value must not cost the whole library migration.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


@dataclass
class Track:
    id: str
    path: str
    title: str
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    cover_file: str = ""
    source: str = "local"
    source_url: str = ""
    added_at: str = field(default_factory=utc_now)
    # 溯源字段（schema v4）：文件丢失时可凭原始地址重新获取。
    # download_url —— 聚合搜索解析出的音频直链（search:* 来源填写）；
    # origin_url  —— 来源页链接，抖音导入即分享视频页（douyin 来源填写）。
    download_url: str = ""
    origin_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_ms": self.duration_ms,
            "cover_file": self.cover_file,
            "source": self.source,
            "source_url": self.source_url,
            "added_at": self.added_at,
            "download_url": self.download_url,
            "origin_url": self.origin_url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Track":
        return cls(
            id=_safe_str(data.get("id", "")),
            path=_safe_str(data.get("path", "")),
            title=_safe_str(data.get("title", "")),
            artist=_safe_str(data.get("artist", "")),
            album=_safe_str(data.get("album", "")),
            duration_ms=_safe_int(data.get("duration_ms", 0)),
            cover_file=_safe_str(data.get("cover_file", "")),
            source=_safe_str(data.get("source", "local"), "local"),
            source_url=_safe_str(data.get("source_url", "")),
            added_at=_safe_str(data.get("added_at", utc_now()), utc_now()),
            download_url=_safe_str(data.get("download_url", "")),
            origin_url=_safe_str(data.get("origin_url", "")),
        )

    # ---- 溯源 ------------------------------------------------------------

    def trace_url(self) -> str:
        """按来源返回最合适的溯源地址；都没有时退回旧的 source_url。"""
        if self.source.startswith("search:"):
            return self.download_url or self.origin_url or self.source_url
        if self.source == "douyin":
            return self.origin_url or self.source_url
        return self.origin_url or self.download_url or self.source_url

    def trace_kind(self) -> str:
        """溯源地址的类型标签，供 UI 展示。"""
        if self.source.startswith("search:"):
            return "音频直链"
        if self.source == "douyin":
            return "抖音视频页"
        return "来源地址"


@dataclass
class Playlist:
    id: str
    name: str
    track_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "track_ids": list(self.track_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Playlist":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            track_ids=[str(value) for value in data.get("track_ids", [])],
            created_at=str(data.get("created_at", utc_now())),
        )


@dataclass
class MusicSource:
    """An HTTP API endpoint that returns a list of individual songs.

    All response fields are addressed by dot-separated JSON paths so one
    source definition works for many JSON shapes without custom code. Sources
    of kind ``demo`` are generated locally and need no network access.
    """

    id: str
    name: str
    kind: str = "http"
    search_url: str = ""
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body_template: str = ""
    list_path: str = "data.songs"
    title_path: str = "title"
    artist_path: str = "artist"
    album_path: str = "album"
    duration_path: str = "duration"
    audio_url_path: str = "url"
    cover_url_path: str = "cover"
    id_path: str = "id"
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "search_url": self.search_url,
            "method": self.method.upper(),
            "headers": dict(self.headers),
            "body_template": self.body_template,
            "list_path": self.list_path,
            "title_path": self.title_path,
            "artist_path": self.artist_path,
            "album_path": self.album_path,
            "duration_path": self.duration_path,
            "audio_url_path": self.audio_url_path,
            "cover_url_path": self.cover_url_path,
            "id_path": self.id_path,
            "enabled": bool(self.enabled),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MusicSource":
        headers = data.get("headers") or {}
        if isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except ValueError:
                headers = {}
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "http") or "http"),
            search_url=str(data.get("search_url", "")),
            method=str(data.get("method", "GET") or "GET").upper(),
            headers={str(k): str(v) for k, v in dict(headers).items()},
            body_template=str(data.get("body_template", "")),
            list_path=str(data.get("list_path", "data.songs")),
            title_path=str(data.get("title_path", "title")),
            artist_path=str(data.get("artist_path", "artist")),
            album_path=str(data.get("album_path", "album")),
            duration_path=str(data.get("duration_path", "duration")),
            audio_url_path=str(data.get("audio_url_path", "url")),
            cover_url_path=str(data.get("cover_url_path", "cover")),
            id_path=str(data.get("id_path", "id")),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class SearchHit:
    """A single song returned by a source (never a playlist)."""

    id: str
    source_id: str
    source_name: str
    title: str
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    audio_url: str = ""
    cover_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
