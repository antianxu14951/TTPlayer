"""Native search for the platforms LX scripts can resolve.

LX custom sources deliberately do not search - they only exchange a known song
for a playable URL. So the player still needs its own search, one per platform,
and the result must carry the ``musicInfo`` shape that scripts expect.

Scripts are defensive: they read ``musicInfo.hash || songmid || songId || id ||
rid || copyrightId``. We therefore emit every alias a platform can have, which
keeps all scripts - including the obfuscated ones - working unchanged.
"""
from __future__ import annotations

import ast
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import requests

from core.models import SearchHit

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

PLATFORM_LABELS: dict[str, str] = {
    "kw": "酷我音乐",
    "kg": "酷狗音乐",
    "tx": "QQ 音乐",
    "wy": "网易云音乐",
    "mg": "咪咕音乐",
}

# Qualities each platform can normally deliver; scripts may narrow this down.
PLATFORM_QUALITYS: dict[str, list[str]] = {
    "kw": ["128k", "320k", "flac"],
    "kg": ["128k", "320k", "flac"],
    "tx": ["128k", "320k", "flac"],
    "wy": ["128k", "320k", "flac"],
}

_TAG_RE = re.compile(r"<[^>]+>")
# Some endpoints double-encode their JSON, so a title arrives as the literal
# text ``\\u0026`` instead of ``&``. Decode those leftovers, repeatedly, because
# a few payloads escape the backslash itself more than once.
_LITERAL_UNICODE_RE = re.compile(r"\\+u([0-9a-fA-F]{4})")
_LITERAL_SLASH_RE = re.compile(r"\\+/")


class SearchError(RuntimeError):
    """One platform failed to answer."""


def _unescape_literal(text: str) -> str:
    value = text
    for _ in range(3):
        decoded = _LITERAL_UNICODE_RE.sub(lambda m: chr(int(m.group(1), 16)), value)
        decoded = _LITERAL_SLASH_RE.sub("/", decoded)
        if decoded == value:
            break
        value = decoded
    return value


def _clean(text: Any) -> str:
    if text is None:
        return ""
    value = _TAG_RE.sub("", str(text)).replace("\xa0", " ")
    value = _unescape_literal(value)
    # ``html.unescape`` twice: ``&amp;quot;`` is common in older QQ payloads.
    value = html.unescape(html.unescape(value))
    return value.strip()


def _get(url: str, *, headers: dict[str, str] | None = None, timeout: float = 12.0) -> requests.Response:
    merged = {"User-Agent": USER_AGENT}
    if headers:
        merged.update(headers)
    try:
        response = requests.get(url, headers=merged, timeout=timeout)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        raise SearchError(str(exc)) from exc


def _json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise SearchError("返回内容不是有效 JSON") from exc


def _hit(
    platform: str,
    song_id: str,
    title: str,
    artist: str,
    album: str,
    duration_ms: int,
    music_info: dict[str, Any],
    cover_url: str = "",
) -> SearchHit:
    return SearchHit(
        id=f"{platform}:{song_id}",
        source_id=platform,
        source_name=PLATFORM_LABELS.get(platform, platform),
        title=_clean(title) or "未知曲目",
        artist=_clean(artist),
        album=_clean(album),
        duration_ms=max(0, duration_ms),
        audio_url="",
        cover_url=cover_url,
        raw={
            "platform": platform,
            "music_info": music_info,
            "qualities": list(PLATFORM_QUALITYS.get(platform, ["128k"])),
        },
    )


# ---------------------------------------------------------------- 酷我

def search_kw(keyword: str, limit: int, timeout: float) -> list[SearchHit]:
    url = (
        "http://search.kuwo.cn/r.s?all={kw}&ft=music&itemset=web_2013&client=kt"
        "&pn=0&rn={rn}&rformat=json&encoding=utf8"
    ).format(kw=requests.utils.quote(keyword), rn=max(1, limit))
    response = _get(url, timeout=timeout)
    text = response.text.strip()
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = ast.literal_eval(text)
        except (ValueError, SyntaxError) as exc:
            raise SearchError("酷我返回格式无法解析") from exc

    hits: list[SearchHit] = []
    for item in (data.get("abslist") or [])[:limit]:
        if not isinstance(item, dict):
            continue
        rid = _clean(item.get("DC_TARGETID") or item.get("MUSICRID") or "")
        if not rid:
            continue
        rid = rid.replace("MUSIC_", "")
        name = _clean(item.get("NAME") or item.get("SONGNAME"))
        artist = _clean(item.get("ARTIST"))
        album = _clean(item.get("ALBUM"))
        try:
            duration = int(float(item.get("DURATION") or 0)) * 1000
        except (TypeError, ValueError):
            duration = 0
        pic = _clean(item.get("web_albumpic_short"))
        cover = f"https://img1.kuwo.cn/star/albumcover/{pic}" if pic else ""
        music_info = {
            "source": "kw",
            "songmid": rid,
            "rid": rid,
            "id": rid,
            "songId": rid,
            "musicrid": f"MUSIC_{rid}",
            "name": name,
            "singer": artist,
            "albumName": album,
            "albumId": _clean(item.get("ALBUMID")),
            "interval": str(duration // 1000),
        }
        hits.append(_hit("kw", rid, name, artist, album, duration, music_info, cover))
    return hits


# ---------------------------------------------------------------- 酷狗

def search_kg(keyword: str, limit: int, timeout: float) -> list[SearchHit]:
    url = (
        "https://songsearch.kugou.com/song_search_v2?keyword={kw}&page=1"
        "&pagesize={rn}&platform=WebFilter&userid=-1&clientver=2000"
        "&iscorrection=1&privilege_filter=0"
    ).format(kw=requests.utils.quote(keyword), rn=max(1, limit))
    payload = _json(_get(url, timeout=timeout))
    rows = ((payload.get("data") or {}).get("lists")) or []

    hits: list[SearchHit] = []
    for item in rows[:limit]:
        if not isinstance(item, dict):
            continue
        file_hash = _clean(item.get("FileHash"))
        if not file_hash:
            continue
        name = _clean(item.get("SongName"))
        artist = _clean(item.get("SingerName"))
        album = _clean(item.get("AlbumName"))
        try:
            duration = int(float(item.get("Duration") or 0)) * 1000
        except (TypeError, ValueError):
            duration = 0
        image = _clean(item.get("Image") or item.get("AlbumImage"))
        cover = image.replace("{size}", "240") if image else ""
        music_info = {
            "source": "kg",
            "hash": file_hash,
            "songmid": file_hash,
            "id": _clean(item.get("MixSongID") or item.get("SongID")),
            "songId": _clean(item.get("SongID")),
            "name": name,
            "singer": artist,
            "albumName": album,
            "albumId": _clean(item.get("AlbumID")),
            "interval": str(duration // 1000),
            "_types": {
                "128k": {"hash": file_hash, "size": None},
                "320k": {"hash": _clean(item.get("HQFileHash")) or file_hash},
                "flac": {"hash": _clean(item.get("SQFileHash")) or file_hash},
            },
        }
        hits.append(_hit("kg", file_hash, name, artist, album, duration, music_info, cover))
    return hits


# ---------------------------------------------------------------- QQ

def search_tx(keyword: str, limit: int, timeout: float) -> list[SearchHit]:
    url = (
        "https://c.y.qq.com/soso/fcgi-bin/client_search_cp?w={kw}&format=json"
        "&n={rn}&p=1&cr=1&new_json=1"
    ).format(kw=requests.utils.quote(keyword), rn=max(1, limit))
    payload = _json(_get(url, headers={"Referer": "https://y.qq.com"}, timeout=timeout))
    rows = (((payload.get("data") or {}).get("song") or {}).get("list")) or []

    hits: list[SearchHit] = []
    for item in rows[:limit]:
        if not isinstance(item, dict):
            continue
        songmid = _clean(item.get("songmid") or item.get("mid"))
        if not songmid:
            continue
        file_info = item.get("file") or {}
        media_mid = _clean(
            item.get("strMediaMid")
            or item.get("media_mid")
            or file_info.get("media_mid")
            or songmid
        )
        singers = item.get("singer") or []
        artist = " / ".join(
            _clean(s.get("name")) for s in singers if isinstance(s, dict) and s.get("name")
        )
        album = _clean(item.get("albumname") or (item.get("album") or {}).get("name"))
        name = _clean(item.get("songname") or item.get("name") or item.get("title"))
        try:
            duration = int(float(item.get("interval") or 0)) * 1000
        except (TypeError, ValueError):
            duration = 0
        album_mid = _clean(item.get("albummid") or (item.get("album") or {}).get("mid"))
        cover = (
            f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg"
            if album_mid
            else ""
        )
        music_info = {
            "source": "tx",
            "songmid": songmid,
            "mid": songmid,
            "id": songmid,
            "strMediaMid": media_mid,
            "mediaId": media_mid,
            "albumMid": _clean(item.get("albummid")),
            "name": name,
            "singer": artist,
            "albumName": album,
            "albumId": str(item.get("albumid") or ""),
            "interval": str(duration // 1000),
        }
        hits.append(_hit("tx", songmid, name, artist, album, duration, music_info, cover))
    return hits


# ---------------------------------------------------------------- 网易云

def search_wy(keyword: str, limit: int, timeout: float) -> list[SearchHit]:
    url = (
        "https://music.163.com/api/search/get/web?s={kw}&type=1&offset=0&limit={rn}"
    ).format(kw=requests.utils.quote(keyword), rn=max(1, limit))
    payload = _json(
        _get(
            url,
            headers={"Referer": "https://music.163.com", "Cookie": "appver=8.7.01"},
            timeout=timeout,
        )
    )
    rows = (((payload.get("result") or {}).get("songs")) or [])

    hits: list[SearchHit] = []
    for item in rows[:limit]:
        if not isinstance(item, dict):
            continue
        song_id = _clean(item.get("id"))
        if not song_id:
            continue
        artists = item.get("artists") or []
        artist = " / ".join(
            _clean(a.get("name")) for a in artists if isinstance(a, dict) and a.get("name")
        )
        album_info = item.get("album") or {}
        album = _clean(album_info.get("name"))
        name = _clean(item.get("name"))
        try:
            duration = int(item.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        pic = _clean(item.get("picUrl") or album_info.get("picUrl"))
        cover = f"{pic}?param=200y200" if pic else ""
        music_info = {
            "source": "wy",
            "songmid": song_id,
            "id": song_id,
            "songId": song_id,
            "name": name,
            "singer": artist,
            "albumName": album,
            "albumId": str(album_info.get("id") or ""),
            "interval": str(duration // 1000),
            "img": _clean(item.get("picUrl")),
        }
        hits.append(_hit("wy", song_id, name, artist, album, duration, music_info, cover))
    return hits


SEARCHERS: dict[str, Callable[[str, int, float], list[SearchHit]]] = {
    "kw": search_kw,
    "kg": search_kg,
    "tx": search_tx,
    "wy": search_wy,
}


def search_platform(
    platform: str,
    keyword: str,
    limit: int = 30,
    timeout: float = 12.0,
) -> list[SearchHit]:
    searcher = SEARCHERS.get(platform)
    if searcher is None:
        raise SearchError("暂不支持搜索该音源")
    return searcher(keyword, limit, timeout)


def search_many(
    platforms: list[str],
    keyword: str,
    limit: int = 30,
    timeout: float = 12.0,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[SearchHit], list[str]]:
    """Search several platforms in parallel; one failure never hides the rest."""
    hits: list[SearchHit] = []
    errors: list[str] = []
    targets = [p for p in platforms if p in SEARCHERS]
    if not targets:
        return hits, ["当前没有可搜索的音源，请先安装并启用音源"]

    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
        futures = {
            pool.submit(search_platform, platform, keyword, limit, timeout): platform
            for platform in targets
        }
        for future in futures:
            try:
                found = future.result()
                hits.extend(found)
                if progress:
                    progress(f"{len(found)} 首")
            except Exception as exc:  # noqa: BLE001 - one platform must not kill the rest
                # No source names in the message: the UI shows these verbatim,
                # and the aggregate view must not reveal where results came from.
                errors.append(f"部分结果未返回（{exc}）")
                if progress:
                    progress("失败")
    return hits, errors
