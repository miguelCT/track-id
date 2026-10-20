"""Resolve caching, yt-dlp error mapping, clip windows, and the ffmpeg command."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.conftest import make_resolved, make_settings
from track_id.audio import (
    build_ffmpeg_args,
    classify_ytdlp_error,
    clip_too_small,
    clip_window,
    extract_clip_to,
    format_ffmpeg_headers,
    parse_hls_playlist,
    resolve_track,
    resolved_from_info,
    select_hls_segments,
)
from track_id.errors import AudioError, StreamExpiredError, UserInputError

PAGE = "https://soundcloud.com/artist/some-set"


def _info(**overrides: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "title": "Closing Set",
        "uploader": "DJ Example",
        "duration": 3600,
        "webpage_url": PAGE,
        "description": "track one",
        "url": "https://cf-media.sndcdn.com/full.m3u8",
        "http_headers": {"User-Agent": "ua", "Referer": "https://soundcloud.com/"},
        "format_id": "hls_aac",
        "formats": [{"format_id": "hls_aac", "url": "https://cf-media.sndcdn.com/full.m3u8"}],
    }
    info.update(overrides)
    return info


def test_clip_window_moves_back_near_the_end() -> None:
    assert clip_window(95, 100, 12) == (88, 12)
    assert clip_window(0, 100, 12) == (0, 12)
    assert clip_window(20, 30, 12) == (18, 12)
    assert clip_window(0, 5, 12) == (0, 5)


def test_clip_window_rejects_a_timestamp_past_the_end() -> None:
    with pytest.raises(UserInputError, match="past the end"):
        clip_window(100, 100, 12)


def test_resolved_info_marks_preview_only_formats() -> None:
    resolved = resolved_from_info(
        PAGE,
        _info(
            duration=214,
            format_id="hls_aac_preview",
            url="https://cf-media.sndcdn.com/preview/0/30/clip.m3u8",
            formats=[
                {
                    "format_id": "hls_aac_preview",
                    "url": "https://cf-media.sndcdn.com/preview/0/30/clip.m3u8",
                }
            ],
        ),
        resolved_at=1.0,
    )
    assert resolved.preview_only is True
    assert resolved.playable_seconds == 30
    assert resolved.duration_seconds == 214


def test_playlist_urls_are_rejected_after_resolution() -> None:
    with pytest.raises(UserInputError, match="playlist"):
        resolved_from_info(
            PAGE, {"_type": "playlist", "entries": [], "title": "A set"}, resolved_at=0
        )


def test_missing_stream_url_tells_you_to_update_ytdlp() -> None:
    with pytest.raises(AudioError, match="yt-dlp"):
        resolved_from_info(PAGE, _info(url=None, formats=[]), resolved_at=0)


def test_description_is_truncated() -> None:
    resolved = resolved_from_info(PAGE, _info(description="x" * 5000), resolved_at=0)
    assert len(resolved.description) <= 4000
    assert resolved.description.endswith("…")


def test_ffmpeg_seeks_before_input_and_passes_headers() -> None:
    args = build_ffmpeg_args(
        "https://cf-media.sndcdn.com/a.m3u8",
        {"User-Agent": "ua", "Referer": "https://soundcloud.com/"},
        42.5,
        12,
        "/tmp/clip.mp3",
    )
    assert args.index("-ss") < args.index("-i")
    assert args.index("-headers") < args.index("-i")
    assert args.index("-t") > args.index("-i")
    assert args[args.index("-ss") + 1] == "42.500"
    assert "-ac" in args and args[args.index("-ac") + 1] == "1"
    assert args[args.index("-ar") + 1] == "44100"
    assert args[args.index("-b:a") + 1] == "128k"
    assert args[-1] == "/tmp/clip.mp3"
    header_blob = args[args.index("-headers") + 1]
    assert "User-Agent: ua\r\n" in header_blob
    assert header_blob.endswith("\r\n")


def test_ffmpeg_header_values_cannot_inject_new_headers() -> None:
    blob = format_ffmpeg_headers({"User-Agent": "ua\r\nX-Evil: injected"})
    assert blob == "User-Agent: ua X-Evil: injected\r\n"
    assert blob.count("\r\n") == 1


def test_non_http_stream_urls_are_refused() -> None:
    with pytest.raises(AudioError, match="http"):
        build_ffmpeg_args("file:///etc/passwd", {}, 0, 12, "/tmp/clip.mp3")


def test_local_playlist_seek_stays_before_input_and_cannot_be_a_url() -> None:
    args = build_ffmpeg_args("/tmp/clip.m3u8", {}, 5, 12, "/tmp/clip.mp3", local_input=True)
    assert args.index("-ss") < args.index("-i")
    whitelist = args[args.index("-protocol_whitelist") + 1]
    assert whitelist.startswith("file,")
    assert "http" not in whitelist
    assert args[args.index("-allowed_extensions") + 1] == "ALL"
    with pytest.raises(AudioError):
        build_ffmpeg_args(
            "https://cdn.example/a.m3u8", {}, 0, 12, "/tmp/clip.mp3", local_input=True
        )


def test_hls_selection_covers_only_the_overlapping_segments() -> None:
    playlist = "\n".join(
        [
            "#EXTM3U",
            '#EXT-X-MAP:URI="https://cdn.example/init.mp4"',
            "#EXTINF:10.0,",
            "https://cdn.example/a.m4s",
            "#EXTINF:10.0,",
            "https://cdn.example/b.m4s",
            "#EXTINF:10.0,",
            "segc.m4s",
            "#EXT-X-ENDLIST",
        ]
    )
    parsed = parse_hls_playlist(playlist, "https://cdn.example/playlist.m3u8")
    assert parsed.segments[0].map_url == "https://cdn.example/init.mp4"
    assert parsed.segments[2].url == "https://cdn.example/segc.m4s"
    chosen, local_start = select_hls_segments(parsed.segments, 15, 12)
    assert [segment.url.rsplit("/", 1)[-1] for segment in chosen] == ["b.m4s", "segc.m4s"]
    assert local_start == 5


def test_hls_master_playlist_picks_the_highest_bandwidth() -> None:
    playlist = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-STREAM-INF:BANDWIDTH=64000",
            "low.m3u8",
            "#EXT-X-STREAM-INF:BANDWIDTH=160000",
            "hi.m3u8",
        ]
    )
    parsed = parse_hls_playlist(playlist, "https://cdn.example/master.m3u8")
    assert parsed.segments == []
    assert parsed.variant_url == "https://cdn.example/hi.m3u8"


def test_hls_rejects_a_private_segment_host() -> None:
    playlist = "#EXTM3U\n#EXTINF:10.0,\nhttp://127.0.0.1/secret.m4s\n"
    with pytest.raises(AudioError, match="non-public"):
        parse_hls_playlist(playlist, "https://cdn.example/playlist.m3u8")


def test_empty_ffmpeg_output_is_too_small() -> None:
    assert clip_too_small(462, 12)
    assert not clip_too_small(120_000, 12)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Private track", UserInputError),
        ("HTTP Error 404: Not Found", UserInputError),
        ("This video is not available in your country", UserInputError),
        ("HTTP Error 429: Too Many Requests", AudioError),
        ("Unable to extract URL", AudioError),
    ],
)
def test_ytdlp_errors_become_safe_messages(message: str, expected: type[Exception]) -> None:
    classified = classify_ytdlp_error(RuntimeError(message))
    assert isinstance(classified, expected)
    assert "https://" not in str(classified)


def test_geo_restricted_error_is_recognized_through_download_error() -> None:
    from yt_dlp.utils import DownloadError, GeoRestrictedError

    geo = GeoRestrictedError("not available in your country")
    wrapped = DownloadError("download failed", exc_info=(type(geo), geo, None))
    classified = classify_ytdlp_error(wrapped)
    assert isinstance(classified, UserInputError)
    assert "geo-blocked" in str(classified)


def test_extractor_breakage_mentions_updating_ytdlp_without_the_url() -> None:
    classified = classify_ytdlp_error(
        RuntimeError("Unable to extract; see https://api.soundcloud.com/tracks/1?client_id=SECRET")
    )
    text = str(classified)
    assert "update yt-dlp" in text
    assert "client_id" not in text
    assert "SECRET" not in text


async def test_resolve_uses_the_cache_until_the_ttl_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_extract(url: str) -> dict[str, Any]:
        calls.append(url)
        return _info()

    monkeypatch.setattr("track_id.audio._extract_info", fake_extract)
    clock = {"now": 1_000.0}
    monkeypatch.setattr("track_id.audio.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("track_id.cache.time.monotonic", lambda: clock["now"])
    settings = make_settings(stream_url_ttl_seconds=300)
    first = await resolve_track(PAGE, settings)
    second = await resolve_track(PAGE, settings)
    assert first is second
    assert calls == [PAGE]
    clock["now"] = 1_400.0
    third = await resolve_track(PAGE, settings)
    assert calls == [PAGE, PAGE]
    assert third is not first
    forced = await resolve_track(PAGE, settings, force=True)
    assert forced is not third
    assert len(calls) == 3


async def test_extract_clip_reports_expired_streams_and_timeouts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    resolved = make_resolved()
    settings = make_settings()

    class Expired:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"Server returned 403 Forbidden for https://cdn.example/a.m3u8?sig=1"

        def kill(self) -> None:
            return None

        async def wait(self) -> int:
            return 1

    async def fake_spawn(*_args: object, **_kwargs: object) -> Expired:
        return Expired()

    monkeypatch.setattr("track_id.audio.asyncio.create_subprocess_exec", fake_spawn)
    with pytest.raises(StreamExpiredError) as caught:
        await extract_clip_to(resolved, 0, 12, str(tmp_path / "clip.mp3"), settings)
    assert "sig=" not in str(caught.value)

    class Hung:
        def __init__(self) -> None:
            self.killed = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(5)
            return b"", b""

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            return -9

    hung = Hung()

    async def fake_hang(*_args: object, **_kwargs: object) -> Hung:
        return hung

    monkeypatch.setattr("track_id.audio.asyncio.create_subprocess_exec", fake_hang)
    from track_id.audio import _run_ffmpeg

    with pytest.raises(AudioError, match="timed out"):
        await _run_ffmpeg(["ffmpeg"], timeout_seconds=0.05)
    assert hung.killed is True
