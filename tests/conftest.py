"""Shared fixtures. The resolve cache is process-global, so every test clears it."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from track_id.audio import clear_caches
from track_id.config import Settings
from track_id.models import ResolvedTrack

SECRET = "a" * 32


@pytest.fixture(autouse=True)
def _reset_audio_cache() -> Iterator[None]:
    clear_caches()
    yield
    clear_caches()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "port": 8080,
        "mcp_path_secret": SECRET,
        "allow_insecure_no_secret": False,
        "recognizers": ("shazam",),
        "clip_seconds": 12.0,
        "retry_offset_seconds": 20.0,
        "max_scan_samples": 30,
        "scan_concurrency": 3,
        "scan_time_budget_seconds": 600.0,
        "stream_url_ttl_seconds": 300.0,
        "ffmpeg_timeout_seconds": 90.0,
        "log_level": "INFO",
        "audd_api_token": None,
        "acr_host": None,
        "acr_access_key": None,
        "acr_access_secret": None,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def make_resolved(
    *,
    duration: float = 3600,
    preview_only: bool = False,
    stream_url: str = "https://cf-media.sndcdn.com/playlist.m3u8",
    page_url: str = "https://soundcloud.com/artist/some-set",
) -> ResolvedTrack:
    playable = min(duration, 30.0) if preview_only else duration
    return ResolvedTrack(
        page_url=page_url,
        title="Closing Set",
        uploader="DJ Example",
        duration_seconds=duration,
        permalink=page_url,
        description="01. Artist - Song",
        stream_url=stream_url,
        http_headers={"User-Agent": "track-id-test"},
        preview_only=preview_only,
        playable_seconds=playable,
        resolved_at=0.0,
    )
