"""Allowlisted SoundCloud resolution and short ffmpeg clips.

Nothing here downloads a full set. yt-dlp is asked only for metadata and a
direct stream URL (`download=False`). HLS clips download only the fragments
that overlap the window. ffmpeg seeks with `-ss` before `-i` and writes a few
seconds of audio.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import httpx

from track_id.cache import TTLCache
from track_id.config import Settings
from track_id.errors import AudioError, StreamExpiredError, UserInputError
from track_id.log import redact_text
from track_id.models import ResolvedTrack
from track_id.timestamps import format_timestamp

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = frozenset(
    {
        "soundcloud.com",
        "www.soundcloud.com",
        "m.soundcloud.com",
        "on.soundcloud.com",
        "api.soundcloud.com",
    }
)
PREVIEW_SECONDS = 30.0
DESCRIPTION_LIMIT = 4000
_PREVIEW_URL = re.compile(r"/(?:preview|playlist)/0/30/")
_cache: TTLCache[ResolvedTrack] = TTLCache()
_locks: dict[str, asyncio.Lock] = {}
_locks_guard = threading.Lock()


def clear_caches() -> None:
    """Drop resolved-stream cache and per-URL locks. Tests call this."""

    _cache.clear()
    _locks.clear()


def validate_soundcloud_url(url: object) -> str:
    """Return a cleaned SoundCloud URL, or raise `UserInputError`.

    Only the public SoundCloud hosts are accepted, over https, with no
    userinfo and no non-default port. That stops this server being used as an
    open fetcher for arbitrary URLs (SSRF). `www.soundcloud.com` is allowed
    because people paste it; it is the same site as `soundcloud.com`.
    """

    if not isinstance(url, str):
        raise UserInputError("URL must be a string.")
    stripped = url.strip()
    if not stripped or any(ord(ch) < 32 or ch.isspace() for ch in stripped):
        raise UserInputError("URL must be a single https SoundCloud link, without spaces.")
    if len(stripped) > 2000 or "\\" in stripped:
        raise UserInputError("That URL is not a usable SoundCloud link.")

    parts = urlsplit(stripped)
    if parts.scheme.lower() != "https":
        raise UserInputError("Only https SoundCloud links are allowed (http is rejected).")
    if parts.username is not None or parts.password is not None:
        raise UserInputError("URLs with an embedded username or password are not allowed.")

    host = parts.hostname
    if host is None or not _allowed_host(host):
        raise UserInputError(
            "That host is not allowed. Use a link on soundcloud.com, www.soundcloud.com, "
            "m.soundcloud.com, on.soundcloud.com, or api.soundcloud.com."
        )
    if parts.port not in (None, 443):
        raise UserInputError("Only the standard https port (443) is allowed.")
    if parts.path in ("", "/"):
        raise UserInputError(
            "That URL has no track path. Paste a link to a specific track or mix."
        )
    return stripped


def soundcloud_path(url: str) -> str:
    """Path only, for logs. Never includes the query string."""

    return urlsplit(url).path


def clip_window(timestamp: float, playable: float, clip_seconds: float) -> tuple[float, float]:
    """Return `(start, length)` so the clip stays inside the playable audio.

    When `timestamp` is within `clip_seconds` of the end, the start moves
    backward instead of reading past the end.
    """

    if playable <= 0:
        raise UserInputError("This track has no playable audio.")
    if timestamp < 0:
        raise UserInputError("Timestamp cannot be negative.")
    if timestamp >= playable:
        raise UserInputError(
            f"Timestamp {format_timestamp(timestamp)} is past the end of the playable "
            f"audio ({format_timestamp(playable)})."
        )
    length = min(float(clip_seconds), playable)
    start = float(timestamp)
    if start + length > playable:
        start = max(0.0, playable - length)
    if start + length > playable:
        length = max(0.1, playable - start)
    return start, length


def truncate_description(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) <= DESCRIPTION_LIMIT:
        return cleaned
    return cleaned[: DESCRIPTION_LIMIT - 1].rstrip() + "…"


async def resolve_track(url: str, settings: Settings, *, force: bool = False) -> ResolvedTrack:
    """Resolve metadata and a direct stream URL, reusing a fresh cache entry."""

    page = validate_soundcloud_url(url)
    if not force:
        cached = _fresh(page, settings.stream_url_ttl_seconds)
        if cached is not None:
            return cached

    async with _lock_for(page):
        if not force:
            cached = _fresh(page, settings.stream_url_ttl_seconds)
            if cached is not None:
                return cached
        started = time.perf_counter()
        info = await asyncio.to_thread(_extract_info, page)
        resolved = resolved_from_info(page, info, resolved_at=time.monotonic())
        _cache.set(page, resolved, ttl=settings.stream_url_ttl_seconds)
        logger.info(
            "resolved",
            extra={
                "event": {
                    "soundcloud_path": soundcloud_path(page),
                    "duration_seconds": resolved.duration_seconds,
                    "preview_only": resolved.preview_only,
                    "resolve_ms": int((time.perf_counter() - started) * 1000),
                }
            },
        )
        return resolved


def resolved_from_info(
    page_url: str, info: dict[str, object], *, resolved_at: float
) -> ResolvedTrack:
    """Build a `ResolvedTrack` from a yt-dlp info dict. Raises user-safe errors."""

    if info.get("_type") in {"playlist", "multi_video"} or (
        isinstance(info.get("entries"), list) and not info.get("url")
    ):
        raise UserInputError(
            "This URL is a SoundCloud playlist of separate tracks, not one audio file. "
            "Pass the URL of a single track or of a DJ mix uploaded as one file."
        )

    duration = info.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, int | float) or duration <= 0:
        raise AudioError("Could not determine how long this track is.")

    preview_only = _preview_only(info)
    duration_seconds = float(duration)
    playable = min(duration_seconds, PREVIEW_SECONDS) if preview_only else duration_seconds
    description = info.get("description") or ""
    if not isinstance(description, str):
        description = str(description)
    permalink = info.get("webpage_url") or info.get("original_url") or page_url
    return ResolvedTrack(
        page_url=page_url,
        title=_text(info.get("title"), "Untitled"),
        uploader=_text(info.get("uploader") or info.get("uploader_id"), "Unknown"),
        duration_seconds=duration_seconds,
        permalink=_text(permalink, page_url),
        description=truncate_description(description),
        stream_url=_stream_url(info),
        http_headers=_headers(info.get("http_headers")),
        preview_only=preview_only,
        playable_seconds=playable,
        resolved_at=resolved_at,
    )


def build_ffmpeg_args(
    stream_url: str,
    headers: Mapping[str, str],
    start: float,
    length: float,
    dest: str,
    *,
    local_input: bool = False,
) -> list[str]:
    """ffmpeg argv. `-ss` is before `-i` so seeking does not decode the whole set.

    `local_input` is a playlist this process just wrote, containing only the
    bytes for this clip. Remote inputs must be http(s); `file://` is refused.
    """

    if local_input:
        if "://" in stream_url or stream_url.startswith("-"):
            raise AudioError("Refusing to read a local clip path that looks like a URL.")
        protocols = "file,crypto,hls"
    else:
        if not stream_url.startswith(("https://", "http://")):
            raise AudioError("Refusing to read a stream URL that is not http(s).")
        protocols = "http,https,tcp,tls,crypto,hls"
    args = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
    ]
    if not local_input:
        header_blob = format_ffmpeg_headers(headers)
        if header_blob:
            args.extend(["-headers", header_blob])
    args.extend(
        [
            "-protocol_whitelist",
            protocols,
        ]
    )
    if local_input:
        # fMP4 segments use extensions ffmpeg's HLS demuxer otherwise refuses.
        args.extend(["-allowed_extensions", "ALL"])
    args.extend(
        [
            "-i",
            stream_url,
            "-t",
            f"{length:.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "44100",
            "-b:a",
            "128k",
            "-f",
            "mp3",
            "-y",
            dest,
        ]
    )
    return args


def format_ffmpeg_headers(headers: Mapping[str, str]) -> str | None:
    """HTTP headers for ffmpeg's `-headers` option. Newlines in values are stripped."""

    lines: list[str] = []
    for key, value in headers.items():
        if not key or any(char in key for char in "\r\n:"):
            continue
        cleaned = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
        if cleaned:
            lines.append(f"{key}: {cleaned}")
    if not lines:
        return None
    return "\r\n".join(lines) + "\r\n"


_EXTINF_RE = re.compile(r"#EXTINF:([0-9]+(?:\.[0-9]+)?)")
_MAP_URI_RE = re.compile(r'URI="([^"]+)"|URI=([^,\s]+)')
_BYTERANGE_RE = re.compile(r"#EXT-X-BYTERANGE:(\d+)(?:@(\d+))?")
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)")
_HLS_PLAYLIST_LIMIT = 2_000_000
_HLS_CLIP_LIMIT = 8_000_000


def clip_too_small(size: int, length: float) -> bool:
    """True when the MP3 cannot contain `length` seconds of audio.

    ffmpeg 6 exits 0 after a failed fMP4 seek and leaves a few hundred bytes
    of header. A real clip at 128 kbps is much larger than this floor.
    """

    floor = max(2048, int(length * 4_000))
    return size < floor


def _looks_like_hls(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith(".m3u8") or path.endswith(".m3u")


class _HlsSegment:
    def __init__(
        self,
        start: float,
        duration: float,
        url: str,
        *,
        map_url: str | None,
        byte_range: tuple[int, int] | None,
    ) -> None:
        self.start = start
        self.duration = duration
        self.url = url
        self.map_url = map_url
        self.byte_range = byte_range


class _HlsPlaylist:
    def __init__(self, segments: list[_HlsSegment], variant_url: str | None) -> None:
        self.segments = segments
        self.variant_url = variant_url


def parse_hls_playlist(text: str, base_url: str) -> _HlsPlaylist:
    """Parse a media playlist, or a master playlist's highest-bandwidth variant."""

    if len(text) > _HLS_PLAYLIST_LIMIT:
        raise AudioError("The stream playlist was larger than expected.")
    segments: list[_HlsSegment] = []
    variants: list[tuple[int, str]] = []
    pending_duration: float | None = None
    pending_range: tuple[int, int] | None = None
    current_map: str | None = None
    cursor = 0.0
    expect_variant = False
    pending_bandwidth = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP"):
            current_map = _resolve_hls_url(base_url, _map_uri(line))
            continue
        if line.startswith("#EXT-X-BYTERANGE"):
            pending_range = _parse_byte_range(line)
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            match = _BANDWIDTH_RE.search(line)
            pending_bandwidth = int(match.group(1)) if match else 0
            expect_variant = True
            continue
        if line.startswith("#EXTINF"):
            match = _EXTINF_RE.match(line)
            if match is None:
                raise AudioError("The stream playlist had an unreadable segment length.")
            pending_duration = float(match.group(1))
            continue
        if line.startswith("#"):
            continue
        url = _resolve_hls_url(base_url, line)
        if expect_variant:
            variants.append((pending_bandwidth, url))
            expect_variant = False
            continue
        if pending_duration is None:
            continue
        segments.append(
            _HlsSegment(
                cursor,
                pending_duration,
                url,
                map_url=current_map,
                byte_range=pending_range,
            )
        )
        cursor += pending_duration
        pending_duration = None
        pending_range = None
    variant_url = None
    if variants and not segments:
        variant_url = max(variants, key=lambda item: item[0])[1]
    return _HlsPlaylist(segments, variant_url)


def select_hls_segments(
    segments: list[_HlsSegment], start: float, length: float
) -> tuple[list[_HlsSegment], float]:
    """Segments overlapping `[start, start + length)`, plus the offset inside the first one."""

    end = start + length
    chosen = [
        segment
        for segment in segments
        if segment.start < end and segment.start + segment.duration > start
    ]
    if not chosen:
        raise AudioError("No audio was published at that timestamp.")
    if len(chosen) > 40:
        raise AudioError("That clip would need more audio than a short sample should.")
    local_start = max(0.0, start - chosen[0].start)
    return chosen, local_start


def _map_uri(line: str) -> str:
    match = _MAP_URI_RE.search(line)
    if match is None:
        raise AudioError("The stream playlist had an unreadable init segment.")
    return match.group(1) or match.group(2)


def _parse_byte_range(line: str) -> tuple[int, int]:
    match = _BYTERANGE_RE.match(line)
    if match is None:
        raise AudioError("The stream playlist had an unreadable byte range.")
    length = int(match.group(1))
    offset = int(match.group(2) or 0)
    return length, offset


def _resolve_hls_url(base_url: str, ref: str) -> str:
    return _assert_public_http_url(urljoin(base_url, ref))


def _assert_public_http_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        raise AudioError("The audio stream used a URL this server will not fetch.")
    host = parts.hostname
    if host is None:
        raise AudioError("The audio stream used a URL this server will not fetch.")
    if host == "localhost" or host.endswith(".localhost") or host.endswith("."):
        raise AudioError("The audio stream pointed at a non-public address.")
    try:
        ip = ip_address(host)
    except ValueError:
        return url
    if not ip.is_global:
        raise AudioError("The audio stream pointed at a non-public address.")
    return url


async def materialize_hls_clip(
    stream_url: str,
    headers: Mapping[str, str],
    start: float,
    length: float,
    directory: str,
) -> tuple[str, float]:
    """Download the init segment and the fragments covering this clip.

    Returns `(local_playlist, seek_inside_that_playlist)`.
    """

    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        playlist_url = _assert_public_http_url(stream_url)
        text = await _get_text(client, playlist_url, headers, _HLS_PLAYLIST_LIMIT)
        parsed = parse_hls_playlist(text, playlist_url)
        if parsed.variant_url and not parsed.segments:
            playlist_url = parsed.variant_url
            text = await _get_text(client, playlist_url, headers, _HLS_PLAYLIST_LIMIT)
            parsed = parse_hls_playlist(text, playlist_url)
        if not parsed.segments:
            raise AudioError("The stream playlist had no audio segments.")
        chosen, local_start = select_hls_segments(parsed.segments, start, length)
        names = await _download_hls_pieces(client, headers, chosen, directory)
    playlist_path = os.path.join(directory, "clip.m3u8")
    body = _local_playlist(chosen, names)
    await asyncio.to_thread(_write_bytes, playlist_path, body.encode("utf-8"))
    return playlist_path, local_start


async def _download_hls_pieces(
    client: httpx.AsyncClient,
    headers: Mapping[str, str],
    segments: list[_HlsSegment],
    directory: str,
) -> list[tuple[str | None, str]]:
    """Download each segment and the init map it needs. Returns `(map_name, seg_name)`."""

    remaining = _HLS_CLIP_LIMIT
    map_names: dict[str, str] = {}
    written: list[tuple[str | None, str]] = []
    for index, segment in enumerate(segments):
        map_name: str | None = None
        if segment.map_url:
            map_name = map_names.get(segment.map_url)
            if map_name is None:
                map_name = "init.mp4" if not map_names else f"init-{len(map_names)}.mp4"
                data, remaining = await _take_bytes(
                    client, segment.map_url, headers, None, remaining
                )
                await asyncio.to_thread(_write_bytes, os.path.join(directory, map_name), data)
                map_names[segment.map_url] = map_name
        seg_name = f"seg-{index:03d}.m4s"
        data, remaining = await _take_bytes(
            client, segment.url, headers, segment.byte_range, remaining
        )
        await asyncio.to_thread(_write_bytes, os.path.join(directory, seg_name), data)
        written.append((map_name, seg_name))
    return written


def _local_playlist(segments: list[_HlsSegment], names: list[tuple[str | None, str]]) -> str:
    target = max(1, int(max(segment.duration for segment in segments) + 0.999))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    current_map: str | None = None
    for segment, (map_name, seg_name) in zip(segments, names, strict=True):
        if map_name != current_map and map_name is not None:
            lines.append(f'#EXT-X-MAP:URI="{map_name}"')
            current_map = map_name
        lines.append(f"#EXTINF:{segment.duration:.3f},")
        lines.append(seg_name)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


async def _get_text(
    client: httpx.AsyncClient, url: str, headers: Mapping[str, str], limit: int
) -> str:
    data, _remaining = await _take_bytes(client, url, headers, None, limit)
    return data.decode("utf-8", "replace")


async def _take_bytes(
    client: httpx.AsyncClient,
    url: str,
    headers: Mapping[str, str],
    byte_range: tuple[int, int] | None,
    remaining: int,
) -> tuple[bytes, int]:
    request_headers = dict(headers)
    if byte_range is not None:
        length, offset = byte_range
        request_headers["Range"] = f"bytes={offset}-{offset + length - 1}"
    try:
        async with client.stream("GET", url, headers=request_headers) as response:
            status = response.status_code
            if status in (401, 403):
                raise StreamExpiredError("The stream URL was rejected (HTTP 403).")
            if status >= 400:
                raise AudioError("Could not read the audio stream.")
            buf = bytearray()
            async for chunk in response.aiter_bytes():
                if len(buf) + len(chunk) > remaining:
                    raise AudioError("The audio clip was larger than expected.")
                buf.extend(chunk)
    except httpx.HTTPError as exc:
        raise AudioError("Could not read the audio stream.") from exc
    if not buf:
        raise AudioError("The audio stream returned an empty file.")
    return bytes(buf), remaining - len(buf)


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)


@asynccontextmanager
async def open_clip(
    resolved: ResolvedTrack,
    start: float,
    length: float,
    settings: Settings,
) -> AsyncIterator[str]:
    """Extract a clip to a temp file and always delete it when the caller is done."""

    directory = tempfile.mkdtemp(prefix="trackid-")
    dest = os.path.join(directory, "clip.mp3")
    try:
        await extract_clip_to(resolved, start, length, dest, settings)
        yield dest
    finally:
        shutil.rmtree(directory, ignore_errors=True)


async def extract_clip_to(
    resolved: ResolvedTrack,
    start: float,
    length: float,
    dest: str,
    settings: Settings,
) -> None:
    """Write one clip to `dest`. Used by the server and by the smoke test.

    SoundCloud's HLS is fMP4. ffmpeg 6 seeks with `-ss` before `-i` on those
    remote playlists and writes an empty file, because it drops the init
    segment. For `.m3u8` URLs the segments that overlap the clip are downloaded
    first (a few hundred KB, not the set) and ffmpeg seeks inside that local
    playlist, still with `-ss` before `-i`. Progressive URLs seek remotely.
    """

    work: str | None = None
    try:
        source = resolved.stream_url
        seek = start
        local_input = False
        if _looks_like_hls(source):
            work = tempfile.mkdtemp(prefix="trackid-hls-")
            source, seek = await materialize_hls_clip(
                source, resolved.http_headers, start, length, work
            )
            local_input = True
        args = build_ffmpeg_args(
            source, resolved.http_headers, seek, length, dest, local_input=local_input
        )
        await _run_ffmpeg(args, timeout_seconds=settings.ffmpeg_timeout_seconds)
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    exists = await asyncio.to_thread(os.path.isfile, dest)
    size = await asyncio.to_thread(os.path.getsize, dest) if exists else 0
    if not exists or clip_too_small(size, length):
        raise AudioError("ffmpeg produced an empty audio clip.")


def classify_ytdlp_error(exc: BaseException) -> UserInputError | AudioError:
    """Turn a yt-dlp failure into a short message with no URL and no stack."""

    causes = list(_iter_causes(exc))
    blob = " ".join(str(cause) for cause in causes).lower()
    geo_blocked = any(type(cause).__name__ == "GeoRestrictedError" for cause in causes) or any(
        phrase in blob
        for phrase in (
            "not available in your country",
            "unavailable in your country",
            "geo restricted",
            "geo-restricted",
        )
    )
    if geo_blocked:
        return UserInputError(
            "This track is geo-blocked and cannot be read from the server's region."
        )
    if "private" in blob:
        return UserInputError("This track is private or no longer shared.")
    if "429" in blob or "rate limit" in blob or "too many requests" in blob:
        return AudioError(
            "SoundCloud is rate-limiting this server. Datacenter IP addresses are often "
            "limited; wait a few minutes and try again."
        )
    if any(token in blob for token in ("404", "not found", "removed", "deleted", "does not exist")):
        return UserInputError("This track was removed or does not exist.")
    if any(token in blob for token in ("go+", "premium", "subscription")):
        return UserInputError(
            "This track is SoundCloud Go+ only. Without a subscription the full audio "
            "is not available, and a 30-second preview often will not identify."
        )
    if "403" in blob or "forbidden" in blob:
        return AudioError(
            "SoundCloud refused access (HTTP 403). Datacenter IP addresses are often "
            "blocked; try again later, or run the server from a residential network."
        )
    if any(
        token in blob
        for token in (
            "unsupported url",
            "unable to extract",
            "no suitable",
            "unable to recognize",
            "extractor",
        )
    ):
        detail = redact_text(str(exc)).replace("\n", " ")[:240]
        return AudioError(
            "yt-dlp could not read this SoundCloud URL. Its extractor is often the part "
            "that breaks when SoundCloud changes; update yt-dlp and redeploy. "
            f"Detail: {detail}"
        )
    detail = redact_text(str(exc)).replace("\n", " ")[:240]
    return AudioError(f"Could not read this SoundCloud track. {detail}")


def _lock_for(page: str) -> asyncio.Lock:
    with _locks_guard:
        lock = _locks.get(page)
        if lock is None:
            lock = asyncio.Lock()
            _locks[page] = lock
        return lock


def _fresh(page: str, ttl: float) -> ResolvedTrack | None:
    cached = _cache.get(page)
    if cached is None:
        return None
    if time.monotonic() - cached.resolved_at >= ttl:
        _cache.invalidate(page)
        return None
    return cached


def _allowed_host(host: str) -> bool:
    if host.endswith(".") or "%" in host or not host.isascii():
        return False
    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        return False
    return host in ALLOWED_HOSTS


def _text(value: object, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _headers(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key or any(char in key for char in "\r\n:"):
            continue
        if raw is None:
            continue
        text = str(raw).replace("\r", "").replace("\n", "").strip()
        if text:
            cleaned[key] = text
    return cleaned


def _is_preview_format(fmt: Mapping[str, object]) -> bool:
    format_id = str(fmt.get("format_id") or "").lower()
    note = str(fmt.get("format_note") or "").lower()
    url = str(fmt.get("url") or "")
    if "preview" in format_id or "preview" in note:
        return True
    return "/preview/" in url or _PREVIEW_URL.search(url) is not None


def _preview_only(info: Mapping[str, object]) -> bool:
    selected = str(info.get("format_id") or "").lower()
    if "preview" in selected:
        return True
    formats = info.get("formats")
    if not isinstance(formats, list):
        return False
    usable = [fmt for fmt in formats if isinstance(fmt, dict) and fmt.get("url")]
    return bool(usable) and all(_is_preview_format(fmt) for fmt in usable)


def _stream_url(info: Mapping[str, object]) -> str:
    direct = info.get("url")
    if isinstance(direct, str) and direct.startswith(("https://", "http://")):
        return direct
    requested = info.get("requested_formats")
    if isinstance(requested, list):
        for fmt in requested:
            if isinstance(fmt, dict):
                url = fmt.get("url")
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    return url
    formats = info.get("formats")
    if isinstance(formats, list):
        http_formats = [
            fmt
            for fmt in formats
            if isinstance(fmt, dict)
            and isinstance(fmt.get("url"), str)
            and str(fmt.get("url")).startswith(("https://", "http://"))
        ]
        for fmt in http_formats:
            if not _is_preview_format(fmt):
                url = fmt.get("url")
                if isinstance(url, str):
                    return url
        if http_formats:
            url = http_formats[-1].get("url")
            if isinstance(url, str):
                return url
    raise AudioError(
        "yt-dlp did not return a stream URL for this track. The SoundCloud extractor "
        "may be out of date — update yt-dlp and redeploy."
    )


def _extract_info(url: str) -> dict[str, object]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise AudioError("yt-dlp is not installed.") from exc

    options = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "cachedir": False,
        "noprogress": True,
        "no_color": True,
        "socket_timeout": 30,
        "retries": 2,
        "extractor_retries": 1,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
    except Exception as exc:
        raise classify_ytdlp_error(exc) from exc
    if not isinstance(info, dict):
        raise AudioError("yt-dlp returned no metadata for this URL.")
    return info


def _iter_causes(exc: BaseException) -> list[BaseException]:
    seen: set[int] = set()
    stack = [exc]
    found: list[BaseException] = []
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        for attr in ("cause", "__cause__"):
            nxt = getattr(current, attr, None)
            if isinstance(nxt, BaseException):
                stack.append(nxt)
        exc_info = getattr(current, "exc_info", None)
        if (
            isinstance(exc_info, tuple)
            and len(exc_info) >= 2
            and isinstance(exc_info[1], BaseException)
        ):
            stack.append(exc_info[1])
    return found


async def _run_ffmpeg(args: list[str], *, timeout_seconds: float) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AudioError("ffmpeg is not installed on this server.") from exc
    try:
        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise AudioError(
            f"ffmpeg timed out after {timeout_seconds:.0f}s while extracting a short clip."
        ) from None
    if process.returncode != 0:
        text = redact_text((stderr or b"").decode("utf-8", "replace")).strip()
        excerpt = text[-400:]
        if _stderr_means_expired(text):
            raise StreamExpiredError(excerpt or "The stream URL was rejected (HTTP 403).")
        if excerpt:
            raise AudioError(f"Could not extract an audio clip. ffmpeg said: {excerpt}")
        raise AudioError("Could not extract an audio clip.")


def _stderr_means_expired(stderr: str) -> bool:
    lowered = stderr.lower()
    return "403" in lowered or "forbidden" in lowered or "expired" in lowered
