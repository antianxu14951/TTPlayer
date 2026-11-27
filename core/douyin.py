from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

import requests

from config.settings import get_cache_dir, load_config

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
    "Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
LEGACY_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/13.0.3 Mobile/15E148 Safari/604.1"
)
DOUYIN_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com")
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

ProgressCallback = Callable[[str, str], None]


class StepError(RuntimeError):
    """A failure tied to one explicit stage of the import pipeline."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.message = message


@dataclass
class StepLog:
    name: str
    detail: str = ""
    ok: bool = False


@dataclass
class DouyinImportResult:
    audio_path: Path
    title: str
    page_url: str
    steps: list[StepLog] = field(default_factory=list)
    cover_bytes: bytes | None = None
    cover_extension: str = ""


def _safe_filename(value: str, fallback: str = "douyin") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value or "").strip(" ._")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = fallback
    if name.upper() in WINDOWS_RESERVED:
        name = f"{name}_file"
    return name[:100]


def _first_url(resource: Any) -> str:
    if isinstance(resource, str):
        return resource
    if isinstance(resource, dict):
        urls = resource.get("url_list") or []
        if urls:
            return str(urls[0])
        if resource.get("url"):
            return str(resource["url"])
    if isinstance(resource, list) and resource:
        return str(resource[0])
    return ""


def extract_share_url(text: str) -> str:
    """Stage 1: pull the Douyin URL out of a share phrase or raw link."""
    candidates = re.findall(r"https?://[^\s\"'<>，。；！？）】》]+", text or "", flags=re.IGNORECASE)
    douyin_urls = []
    for candidate in candidates:
        url = candidate.rstrip(".,;:!?，。；！？）】》")
        host = (urlparse(url).hostname or "").lower()
        if any(host == suffix or host.endswith("." + suffix) for suffix in DOUYIN_HOST_SUFFIXES):
            douyin_urls.append(url)
    if not douyin_urls:
        raise StepError("步骤1 提取分享链接", "未在文本中找到抖音链接，请粘贴完整分享口令或短链")
    return douyin_urls[0]


def extract_aweme_id(value: str) -> str:
    if not value:
        return ""
    patterns = (
        r"/(?:share/video|video|note|discovery/post)/(\d{15,25})",
        r"(?:aweme_id|item_ids|modal_id|itemId)=(\d{15,25})",
        r'"aweme_id"\s*:\s*"?(\d{15,25})"?',
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return ""


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": MOBILE_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.iesdouyin.com/",
        }
    )
    return session


def resolve_video_page_url(
    url: str, session: requests.Session, timeout: float
) -> tuple[str, str, str]:
    """Stage 2: share short link -> canonical URL, aweme id and redirect-page HTML."""
    aweme_id = extract_aweme_id(url)
    final_url = url
    page_html = ""
    if not aweme_id:
        try:
            response = session.get(url, allow_redirects=True, timeout=timeout)
            response.raise_for_status()
            final_url = response.url
            page_html = response.text
            aweme_id = extract_aweme_id(final_url) or extract_aweme_id(page_html[:200_000])
        except requests.RequestException as exc:
            raise StepError("步骤2 解析视频页面", f"访问分享链接失败：{exc}") from exc
    if not aweme_id:
        raise StepError(
            "步骤2 解析视频页面",
            "无法从分享链接中提取视频 ID；请确认粘贴的是单个视频的分享口令，而不是主页或合集链接",
        )
    return final_url, aweme_id, page_html


def _balanced_json_object(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _find_item(node: Any) -> dict[str, Any] | None:
    if isinstance(node, dict):
        video_info = node.get("videoInfoRes")
        if isinstance(video_info, dict):
            items = video_info.get("item_list") or []
            if items and isinstance(items[0], dict):
                return items[0]
        detail = node.get("aweme_detail")
        if isinstance(detail, dict):
            return detail
        items = node.get("item_list")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        if isinstance(node.get("video"), dict) and ("aweme_id" in node or "desc" in node):
            return node
        for child in node.values():
            found = _find_item(child)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_item(child)
            if found is not None:
                return found
    return None


def _parse_item_from_html(html: str) -> dict[str, Any] | None:
    markers = (
        "window._ROUTER_DATA =",
        "window._ROUTER_DATA=",
        "window.__INITIAL_STATE__ =",
        "window.__INITIAL_STATE__=",
    )
    for marker in markers:
        start = html.find(marker)
        if start < 0:
            continue
        cursor = start + len(marker)
        while cursor < len(html) and html[cursor].isspace():
            cursor += 1
        if cursor >= len(html) or html[cursor] not in "{[":
            continue
        script_end = html.find("</script>", cursor)
        segment = html[cursor : script_end if script_end >= 0 else len(html)].strip().rstrip(";")
        try:
            data = json.loads(unescape(segment))
        except json.JSONDecodeError:
            if segment.startswith("{"):
                raw = _balanced_json_object(segment, 0)
                if not raw:
                    continue
                try:
                    data = json.loads(unescape(raw))
                except json.JSONDecodeError:
                    continue
            else:
                continue
        item = _find_item(data)
        if item is not None:
            return item

    for key in ('"aweme_detail"', '"awemeDetail"'):
        index = html.find(key)
        if index < 0:
            continue
        brace = html.find("{", index)
        if brace < 0:
            continue
        raw = _balanced_json_object(html, brace)
        if not raw:
            continue
        try:
            item = json.loads(unescape(raw))
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            return item
    return None


def fetch_item_detail(
    aweme_id: str, session: requests.Session, timeout: float, page_html: str = ""
) -> dict[str, Any]:
    """Stage 3: obtain the public item JSON anonymously from Douyin pages."""
    config = load_config()
    retries = max(1, int(config.get("douyin_fetch_retries", 3)))
    delay = max(0.0, float(config.get("douyin_retry_delay_seconds", 2.0)))
    user_agents = (LEGACY_MOBILE_UA, MOBILE_UA, DESKTOP_UA)
    attempts = [
        f"https://www.iesdouyin.com/share/video/{aweme_id}/",
        f"https://m.douyin.com/share/video/{aweme_id}",
        f"https://www.douyin.com/video/{aweme_id}",
    ]
    blocked = False
    for round_index in range(retries):
        session.headers["User-Agent"] = user_agents[round_index % len(user_agents)]
        if page_html:
            item = _parse_item_from_html(page_html)
            if item is not None:
                return item
        for url in attempts:
            try:
                response = session.get(url, timeout=timeout)
                if response.status_code != 200:
                    continue
                lower_page = response.text.lower()
                if any(
                    marker in lower_page
                    for marker in ("captcha", "argussecurity", "滑块验证", "verify_center")
                ):
                    blocked = True
                item = _parse_item_from_html(response.text)
                if item is not None:
                    return item
            except requests.RequestException:
                continue
        if round_index + 1 < retries and delay:
            time.sleep(delay)
    try:
        legacy = session.get(
            "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/",
            params={"item_ids": aweme_id},
            timeout=timeout,
        )
        if legacy.status_code == 200:
            payload = legacy.json()
            items = payload.get("item_list") or []
            if items and isinstance(items[0], dict):
                return items[0]
    except (requests.RequestException, ValueError):
        pass
    detail = (
        f"匿名访问连续 {retries} 次被抖音风控页面拦截（验证码/安全校验）；"
        "当前匿名模式无法取得作品数据，请稍后重试"
        if blocked
        else f"匿名状态下连续 {retries} 次未取到作品数据，可能是链接失效或抖音已移除页面内的公开数据"
    )
    raise StepError("步骤3 获取作品数据", detail)


def _build_play_url(video_id: str, quality: str) -> str:
    return (
        "https://aweme.snssdk.com/aweme/v1/play/"
        f"?video_id={quote(video_id)}&ratio={quote(quality)}&line=0"
    )


def _video_candidates(item: dict[str, Any], quality: str) -> list[str]:
    video = item.get("video") or {}
    candidates: list[str] = []

    def add_play_addr(play_addr: Any) -> None:
        if not isinstance(play_addr, dict):
            return
        for url in play_addr.get("url_list") or []:
            if url:
                candidates.append(str(url).replace("playwm", "play"))
        uri = play_addr.get("uri") or play_addr.get("uri_v2")
        if uri:
            candidates.append(_build_play_url(str(uri), quality))

    add_play_addr(video.get("play_addr"))
    add_play_addr(video.get("play_addr_h264"))
    for bit_rate in video.get("bit_rate") or []:
        if isinstance(bit_rate, dict):
            add_play_addr(bit_rate.get("play_addr"))
    return list(dict.fromkeys(candidates))


def _looks_like_video(response: requests.Response) -> bool:
    content_type = (response.headers.get("Content-Type") or "").lower()
    final_url = unquote(response.url).lower()
    return (
        "video/" in content_type
        or "octet-stream" in content_type
        or any(extension in final_url for extension in (".mp4", ".mov", ".m4v", ".webm"))
    )


def extract_real_video_url(
    item: dict[str, Any], session: requests.Session, quality: str, timeout: float
) -> str:
    """Stage 4: resolve a directly downloadable video (MP4) URL."""
    candidates = _video_candidates(item, quality)
    for url in candidates:
        try:
            response = session.get(
                url,
                headers={"Referer": "https://www.iesdouyin.com/", "User-Agent": MOBILE_UA},
                allow_redirects=True,
                stream=True,
                timeout=timeout,
            )
            status = response.status_code
            final_url = response.url
            valid = status == 200 and _looks_like_video(response)
            response.close()
            if valid:
                return final_url
        except requests.RequestException:
            continue
    raise StepError(
        "步骤4 获取真实视频地址",
        "候选播放地址均不可用，抖音可能要求登录校验或已更换接口参数；本次未使用 Cookie，属于匿名模式的已知限制",
    )


def download_video(
    url: str,
    destination: Path,
    session: requests.Session,
    timeout: float,
    progress: ProgressCallback | None = None,
) -> Path:
    """Stage 5/1: stream the MP4 to a temporary file and verify its header."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with session.get(
            url,
            headers={"Referer": "https://www.iesdouyin.com/", "User-Agent": MOBILE_UA},
            stream=True,
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0)
            written = 0
            with destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    if progress and total:
                        percent = min(100, int(written * 100 / total))
                        progress("步骤5 导入音频", f"已下载 {percent}%")
    except requests.RequestException as exc:
        raise StepError("步骤5 导入音频", f"下载失败：{exc}") from exc
    if written < 1024:
        raise StepError("步骤5 导入音频", "下载内容过小，可能被风控页面替代")
    with destination.open("rb") as handle:
        header = handle.read(16)
    if b"ftyp" not in header:
        raise StepError("步骤5 导入音频", "下载内容不是有效 MP4 文件，可能被风控拦截")
    return destination


def _resolve_ffmpeg(configured: str) -> str:
    """内置 ffmpeg 优先，其次配置路径，最后系统 PATH。"""
    from core.ffmpeg_bin import resolve_ffmpeg

    try:
        return resolve_ffmpeg()
    except FileNotFoundError as exc:
        raise StepError("步骤5 导入音频", str(exc)) from exc


def convert_mp4_to_mp3(source: Path, destination: Path) -> Path:
    """Stage 5/2: extract audio with FFmpeg; fall back to a generic mp3 encoder."""
    from core.ffmpeg_bin import no_console_kwargs

    config = load_config()
    ffmpeg = _resolve_ffmpeg(str(config.get("ffmpeg_path", "ffmpeg")))
    destination.parent.mkdir(parents=True, exist_ok=True)
    base = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-vn",
    ]
    attempts = [
        base + ["-codec:a", "libmp3lame", "-q:a", "2", str(destination)],
        base + ["-codec:a", "mp3", str(destination)],
    ]
    last_error = ""
    for command in attempts:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                **no_console_kwargs(),
            )
        except OSError as exc:
            raise StepError("步骤5 导入音频", f"无法执行 ffmpeg：{exc}") from exc
        if result.returncode == 0 and destination.exists() and destination.stat().st_size >= 1024:
            return destination
        last_error = (result.stderr or "").strip()[-400:]
    raise StepError("步骤5 导入音频", f"ffmpeg 转换失败：{last_error or '未知错误'}")


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise StepError("步骤5 导入音频", "无法生成不重复的音频文件名")


def fetch_cover(item: dict[str, Any], session: requests.Session, timeout: float) -> tuple[bytes | None, str]:
    video = item.get("video") or {}
    cover_url = _first_url(video.get("cover")) or _first_url(video.get("origin_cover"))
    if not cover_url:
        return None, ""
    try:
        response = session.get(cover_url, timeout=timeout)
        response.raise_for_status()
        content_type = (response.headers.get("Content-Type") or "").lower()
        extension = ".png" if "png" in content_type else ".jpg"
        return response.content, extension
    except requests.RequestException:
        return None, ""


def import_share(text: str, progress: ProgressCallback | None = None) -> DouyinImportResult:
    """Run the complete anonymous share-code -> MP3 pipeline."""
    config = load_config()
    timeout = float(config.get("request_timeout_seconds", 45))
    download_timeout = float(config.get("download_timeout_seconds", 120))
    quality = str(config.get("douyin_quality", "1080p"))
    cache_dir = get_cache_dir()
    session = create_session()
    steps: list[StepLog] = []
    temp_mp4: Path | None = None

    def report(name: str, detail: str) -> None:
        steps.append(StepLog(name=name, detail=detail, ok=True))
        if progress:
            progress(name, detail)

    try:
        share_url = extract_share_url(text)
        report("步骤1 提取分享链接", share_url)

        page_url, aweme_id, page_html = resolve_video_page_url(share_url, session, timeout)
        report("步骤2 解析视频页面", f"视频 ID：{aweme_id}")

        item = fetch_item_detail(aweme_id, session, timeout, page_html)
        title = _safe_filename(
            str(item.get("desc") or item.get("share_info", {}).get("share_title") or f"抖音视频_{aweme_id}")
        )
        report("步骤3 获取作品数据", title)

        real_url = extract_real_video_url(item, session, quality, timeout)
        report("步骤4 获取真实视频地址", "已取得可下载的 MP4 地址")

        cache_dir.mkdir(parents=True, exist_ok=True)
        temp_mp4 = cache_dir / f"douyin_{aweme_id}_{int(time.time())}.mp4"
        download_retries = max(1, int(config.get("douyin_download_retries", 3)))
        download_delay = max(0.0, float(config.get("douyin_download_retry_delay_seconds", 2.0)))
        for attempt in range(download_retries):
            try:
                download_video(real_url, temp_mp4, session, download_timeout, progress)
                break
            except StepError:
                if temp_mp4.exists():
                    temp_mp4.unlink()
                if attempt + 1 == download_retries:
                    raise
                if progress:
                    progress("步骤5 导入音频", f"第 {attempt + 2} 次尝试")
                if download_delay:
                    time.sleep(download_delay)
        # 下载与转码属于同一个「导入音频」阶段：前者报下载百分比，后者只
        # 更新提示，不追加第二条同名步骤记录。
        if progress:
            size_mb = temp_mp4.stat().st_size / 1024 / 1024
            progress("步骤5 导入音频", f"已下载 {size_mb:.2f} MB，正在提取音轨")

        cache_dir.mkdir(parents=True, exist_ok=True)
        output = _unique_path(cache_dir / f"{title}.mp3")
        convert_mp4_to_mp3(temp_mp4, output)
        report("步骤5 导入音频", output.name)

        cover_bytes, cover_extension = fetch_cover(item, session, timeout)
        # 转换成功后原始 MP4 必须删除：.cache 中只保留可直接播放的 MP3。
        if temp_mp4 is not None and temp_mp4.exists():
            temp_mp4.unlink(missing_ok=True)
            temp_mp4 = None
        return DouyinImportResult(
            audio_path=output,
            title=title,
            page_url=page_url,
            steps=steps,
            cover_bytes=cover_bytes,
            cover_extension=cover_extension,
        )
    except StepError as exc:
        steps.append(StepLog(name=exc.step, detail=exc.message, ok=False))
        if temp_mp4 is not None and temp_mp4.exists():
            temp_mp4.unlink(missing_ok=True)
        raise
    except Exception as exc:
        steps.append(StepLog(name="导入失败", detail=str(exc), ok=False))
        if temp_mp4 is not None and temp_mp4.exists():
            temp_mp4.unlink(missing_ok=True)
        raise StepError("导入失败", str(exc)) from exc
