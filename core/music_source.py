"""Aggregated single-song search and import.

A source is an HTTP API that returns a JSON list of songs. Users configure the
endpoint and JSON field paths in Settings; no code changes are needed for a new
source. The built-in ``demo`` source works fully offline so the whole pipeline
can be exercised without any external service.

Only individual songs are returned - playlist/album endpoints are intentionally
out of scope. Imported songs are converted to MP3 and stored in the cache
directory, then added to the same library as local files and Douyin imports.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests

from config.settings import load_config, get_cache_dir, TEMP_DIR
from core.library import Library
from core.metadata import build_track_from_file
from core.models import MusicSource, SearchHit, Track

ProgressCallback = Callable[[str, str], None]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

# Offline demo catalog. audio_url uses a pseudo scheme resolved locally by
# download_audio(), so testing never touches the network.
DEMO_CATALOG: tuple[dict[str, Any], ...] = (
    {"id": "demo-01", "title": "晨雾", "artist": "演示歌手 A", "album": "雾色", "duration": 184000, "url": "demo://demo-01"},
    {"id": "demo-02", "title": "月下漫游", "artist": "演示歌手 B", "album": "夜行", "duration": 212000, "url": "demo://demo-02"},
    {"id": "demo-03", "title": "海边的风", "artist": "演示歌手 A", "album": "潮汐", "duration": 156000, "url": "demo://demo-03"},
    {"id": "demo-04", "title": "城市微光", "artist": "演示歌手 C", "album": "街角", "duration": 201000, "url": "demo://demo-04"},
    {"id": "demo-05", "title": "雨天签名", "artist": "演示歌手 B", "album": "夜行", "duration": 173000, "url": "demo://demo-05"},
)


class SourceError(RuntimeError):
    """A failure tied to one source during search or import."""


def demo_source() -> MusicSource:
    """Return the built-in offline demonstration source."""
    return MusicSource(
        id="builtin-demo",
        name="演示音源（离线）",
        kind="demo",
        enabled=True,
    )


def default_sources() -> list[MusicSource]:
    """Sources used on first run: demo only, so nothing reaches the network."""
    return [demo_source()]


def get_path(data: Any, path: str) -> Any:
    """Resolve a dot-separated JSON path, allowing numeric list indices.

    ``get_path({"data": {"songs": [{"name": "x"}]}}, "data.songs.0.name")``
    returns ``"x"``. Empty paths return the original object.
    """
    if not path:
        return data
    node = data
    for raw in path.split("."):
        part = raw.strip()
        if not part:
            continue
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, (list, tuple)) and part.lstrip("-").isdigit():
            index = int(part)
            if -len(node) <= index < len(node):
                node = node[index]
            else:
                return None
        else:
            return None
        if node is None:
            return None
    return node


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " / ".join(str(item) for item in value if item)
    return str(value)


def parse_duration(value: Any) -> int:
    """Normalize seconds, milliseconds or mm:ss into milliseconds."""
    if isinstance(value, (int, float)):
        number = float(value)
        # Durations below 1000 are almost certainly seconds.
        return int(round(number * 1000 if number < 1000 else number))
    text = _text(value).strip()
    if not text:
        return 0
    if re.fullmatch(r"\d+(?::\d{1,2}){1,2}", text):
        parts = [int(part) for part in text.split(":")]
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds * 1000
    match = re.search(r"\d+(?:\.\d+)?", text)
    return parse_duration(float(match.group())) if match else 0


def _hit_id(source_id: str, raw: dict[str, Any], title: str, artist: str) -> str:
    seed = f"{source_id}|{raw.get('id', '')}|{title}|{artist}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def _hit_from_raw(source: MusicSource, raw: dict[str, Any]) -> SearchHit | None:
    title = _text(get_path(raw, source.title_path)).strip()
    if not title:
        return None
    artist = _text(get_path(raw, source.artist_path)).strip()
    audio_url = _text(get_path(raw, source.audio_url_path)).strip()
    raw_id = _text(get_path(raw, source.id_path)).strip()
    return SearchHit(
        id=_hit_id(source.id, raw, title, artist),
        source_id=source.id,
        source_name=source.name,
        title=title,
        artist=artist,
        album=_text(get_path(raw, source.album_path)).strip(),
        duration_ms=parse_duration(get_path(raw, source.duration_path)),
        audio_url=audio_url or (f"{source.id}://{raw_id}" if raw_id else ""),
        cover_url=_text(get_path(raw, source.cover_url_path)).strip(),
        raw=raw,
    )


def _demo_hits(source: MusicSource, keyword: str) -> list[SearchHit]:
    needle = keyword.strip().casefold()
    catalog = DEMO_CATALOG
    if needle:
        catalog = tuple(
            item for item in catalog
            if needle in f"{item['title']} {item['artist']} {item.get('album', '')}".casefold()
        )
    return [
        hit
        for raw in catalog
        if (hit := _hit_from_raw(source, dict(raw))) is not None
    ]


def search_source(source: MusicSource, keyword: str, timeout: float) -> list[SearchHit]:
    """Search one source for individual songs. Raises SourceError on failure."""
    if source.kind == "demo":
        return _demo_hits(source, keyword)
    if not source.search_url.strip():
        raise SourceError(f"音源「{source.name}」未配置搜索地址")
    url = source.search_url.replace("{keyword}", quote(keyword))
    headers = {"User-Agent": DEFAULT_UA, "Accept": "application/json"}
    headers.update(source.headers)
    try:
        if source.method.upper() == "POST":
            body = source.body_template.replace("{keyword}", keyword)
            try:
                import json

                payload = json.loads(body) if body.strip() else {}
            except ValueError:
                payload = body
            response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        else:
            response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise SourceError(f"音源「{source.name}」请求失败：{exc}") from exc
    except ValueError as exc:
        raise SourceError(f"音源「{source.name}」返回的不是有效 JSON：{exc}") from exc

    rows = get_path(data, source.list_path)
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        raise SourceError(
            f"音源「{source.name}」返回结构中没有歌曲列表（路径 {source.list_path or '根'}）"
        )
    hits: list[SearchHit] = []
    for row in rows:
        if isinstance(row, dict) and (hit := _hit_from_raw(source, row)) is not None:
            hits.append(hit)
    return hits


def _dedupe_key(hit: SearchHit) -> tuple[str, str]:
    return (hit.title.strip().casefold(), hit.artist.strip().casefold())


@dataclass
class AggregateResult:
    hits: list[SearchHit] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    searched: int = 0


def search_aggregate(
    sources: list[MusicSource],
    keyword: str,
    timeout: float = 15.0,
    progress: ProgressCallback | None = None,
    limit_per_source: int = 0,
) -> AggregateResult:
    """Search all enabled sources, deduplicate songs, isolate per-source errors.

    ``limit_per_source`` caps how many songs one source contributes (0 = no
    cap) so a chatty API cannot flood the result list.
    """
    result = AggregateResult()
    active = [source for source in sources if source.enabled]
    seen: dict[tuple[str, str], SearchHit] = {}
    for index, source in enumerate(active, start=1):
        if progress:
            progress("正在搜索", f"{index}/{len(active)} · {source.name}")
        try:
            hits = search_source(source, keyword, timeout)
        except SourceError as exc:
            result.errors.append(str(exc))
            continue
        except Exception as exc:  # pragma: no cover - defensive per-source boundary
            result.errors.append(f"音源「{source.name}」搜索异常：{exc}")
            continue
        result.searched += 1
        used = 0
        for hit in hits:
            if limit_per_source and used >= limit_per_source:
                break
            key = _dedupe_key(hit)
            existing = seen.get(key)
            if existing is None:
                seen[key] = hit
                result.hits.append(hit)
                used += 1
            elif existing.source_name != hit.source_name:
                existing.source_name = f"{existing.source_name} · {hit.source_name}"
    if progress:
        progress("搜索完成", f"{len(active)} 个音源，找到 {len(result.hits)} 首单曲")
    return result


def _safe_filename(value: str, fallback: str = "search") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value or "").strip(" ._")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = fallback
    if name.upper() in WINDOWS_RESERVED:
        name = f"{name}_file"
    return name[:100]


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise SourceError("无法生成不重复的音频文件名")


def _resolve_ffmpeg() -> str:
    """内置 ffmpeg 优先，其次配置路径，最后系统 PATH。"""
    from core.ffmpeg_bin import resolve_ffmpeg

    try:
        return resolve_ffmpeg()
    except FileNotFoundError as exc:
        raise SourceError(str(exc)) from exc


def _run_ffmpeg(args: list[str], error: str) -> None:
    from core.ffmpeg_bin import no_console_kwargs

    ffmpeg = _resolve_ffmpeg()
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **no_console_kwargs(),
        )
    except OSError as exc:
        raise SourceError(f"无法执行 ffmpeg：{exc}") from exc
    if completed.returncode != 0:
        raise SourceError(f"{error}：{(completed.stderr or '').strip()[-300:]}")


def _generate_demo_audio(hit: SearchHit, destination: Path) -> Path:
    """Create a short silent MP3 so the demo source works without network."""
    duration = min(8.0, max(2.0, hit.duration_ms / 1000 or 4.0))
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", f"{duration:.1f}",
            "-codec:a", "libmp3lame", "-q:a", "5", str(destination),
        ],
        "演示音频生成失败",
    )
    return destination


def download_audio(
    hit: SearchHit,
    destination: Path,
    progress: ProgressCallback | None = None,
) -> Path:
    """Download one song and produce a playable MP3 at ``destination``."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if hit.audio_url.startswith("demo://"):
        if progress:
            progress("生成演示音频", hit.title)
        return _generate_demo_audio(hit, destination)

    config = load_config()
    timeout = float(config.get("download_timeout_seconds", 120))
    suffix = Path(hit.audio_url.split("?", 1)[0]).suffix.lower()
    temp = TEMP_DIR / f"source_{hit.id}_{int(time.time())}{suffix or '.bin'}"
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if progress:
            progress("下载音频", hit.title)
        with requests.get(
            hit.audio_url,
            headers={"User-Agent": DEFAULT_UA, "Referer": hit.source_id},
            stream=True,
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0)
            written = 0
            with temp.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    if progress and total:
                        percent = min(100, written * 100 // total)
                        progress("下载音频", f"{hit.title} · {percent}%")
        if written < 1024:
            raise SourceError(f"《{hit.title}》下载内容过小，可能不是有效音频")

        is_mp3 = suffix == ".mp3" or temp.read_bytes()[:3] == b"ID3"
        if is_mp3:
            shutil.copyfile(temp, destination)
        else:
            if progress:
                progress("转换为 MP3", hit.title)
            _run_ffmpeg(
                ["-i", str(temp), "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(destination)],
                f"《{hit.title}》转换失败",
            )
        if not destination.exists() or destination.stat().st_size < 1024:
            raise SourceError(f"《{hit.title}》生成的音频无效")
        return destination
    except requests.RequestException as exc:
        raise SourceError(f"《{hit.title}》下载失败：{exc}") from exc
    finally:
        temp.unlink(missing_ok=True)


@dataclass
class ImportResult:
    track: Track | None = None
    error: str = ""


def prepare_audio(
    hit: SearchHit,
    progress: ProgressCallback | None = None,
) -> Path:
    """Download/convert one hit into a cached MP3 and return its path."""
    cache = get_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    output = _unique_path(cache / f"{_safe_filename(hit.title)}.mp3")
    if progress:
        progress("准备导入", hit.title)
    download_audio(hit, output, progress)
    return output


def track_from_audio(hit: SearchHit, path: Path) -> Track:
    """Read metadata and build a Track. Call on the UI thread (Mutagen)."""
    track = build_track_from_file(
        path,
        source=f"search:{hit.source_id}",
        source_url=hit.audio_url,
    )
    if hit.title:
        track.title = hit.title
    if hit.artist:
        track.artist = hit.artist
    if hit.album:
        track.album = hit.album
    if hit.duration_ms:
        track.duration_ms = hit.duration_ms
    return track


def import_hit(
    hit: SearchHit,
    library: Library,
    progress: ProgressCallback | None = None,
) -> ImportResult:
    """Convert a search hit into a cached MP3 and add it to the library."""
    try:
        output = prepare_audio(hit, progress)
        track = track_from_audio(hit, output)
        if not library.add_track(track):
            output.unlink(missing_ok=True)
            return ImportResult(error=f"《{hit.title}》已存在于媒体库")
        if progress:
            progress("导入完成", hit.title)
        return ImportResult(track=track)
    except SourceError as exc:
        return ImportResult(error=str(exc))
    except Exception as exc:  # pragma: no cover - defensive UI boundary
        return ImportResult(error=f"《{hit.title}》导入失败：{exc}")
