from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.flac import FLAC

from config.settings import COVER_DIR
from core.models import Track, utc_now

_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _extension_from_mime(mime: str, fallback: str = ".jpg") -> str:
    return _MIME_EXTENSIONS.get((mime or "").lower(), fallback)


def read_tags(path: Path) -> dict[str, Any]:
    """Read title/artist/album/duration through Mutagen's easy interface."""
    result: dict[str, Any] = {"title": "", "artist": "", "album": "", "duration_ms": 0}
    try:
        audio = MutagenFile(str(path), easy=True)
        if audio is None:
            return result
        tags = audio.tags or {}

        def first(key: str) -> str:
            values = tags.get(key)
            if isinstance(values, (list, tuple)) and values:
                return str(values[0])
            if values:
                return str(values)
            return ""

        result["title"] = first("title")
        result["artist"] = first("artist")
        result["album"] = first("album")
        if audio.info is not None:
            result["duration_ms"] = int(round(float(audio.info.length or 0) * 1000))
    except Exception:
        pass
    return result


def read_cover(path: Path) -> tuple[bytes | None, str]:
    """Extract embedded album art without relying on a specific audio format."""
    try:
        audio = MutagenFile(str(path))
        if audio is None or audio.tags is None:
            return None, ""
        tags = audio.tags

        if hasattr(tags, "getall"):
            for frame in tags.getall("APIC"):
                return bytes(frame.data), _extension_from_mime(getattr(frame, "mime", ""))

        if isinstance(audio, FLAC):
            pictures = getattr(audio, "pictures", [])
            if pictures:
                picture = pictures[0]
                return bytes(picture.data), _extension_from_mime(getattr(picture, "mime", ""))

        if "covr" in tags:
            cover = tags["covr"][0]
            is_png = getattr(cover, "imageformat", None) == 14
            return bytes(cover), (".png" if is_png else ".jpg")
    except Exception:
        pass
    return None, ""


def save_cover(track_id: str, data: bytes, extension: str) -> str:
    """Store cover art using a content-derived filename; return the filename."""
    if not data:
        return ""
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    extension = extension if extension.startswith(".") and len(extension) <= 6 else ".jpg"
    digest = hashlib.sha1(track_id.encode("utf-8")).hexdigest()[:20]
    filename = f"{digest}{extension}"
    target = COVER_DIR / filename
    if not target.exists():
        target.write_bytes(data)
    return filename


def build_track_from_file(
    path: Path | str,
    *,
    source: str = "local",
    source_url: str = "",
    cover_bytes: bytes | None = None,
    cover_extension: str = "",
) -> Track:
    file_path = Path(path).resolve()
    tags = read_tags(file_path)
    track_id = uuid.uuid4().hex
    embedded_cover, embedded_extension = read_cover(file_path)
    payload = cover_bytes if cover_bytes else embedded_cover
    extension = cover_extension if cover_bytes else embedded_extension
    cover_file = save_cover(track_id, payload or b"", extension)
    return Track(
        id=track_id,
        path=str(file_path),
        title=tags.get("title") or file_path.stem,
        artist=tags.get("artist") or "未知歌手",
        album=tags.get("album") or "",
        duration_ms=int(tags.get("duration_ms", 0)),
        cover_file=cover_file,
        source=source,
        source_url=source_url,
        added_at=utc_now(),
    )
