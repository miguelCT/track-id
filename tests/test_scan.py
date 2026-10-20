"""Scan dedup, chunking, time budget, clip windows, and stream re-resolve."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from tests.conftest import make_resolved, make_settings
from track_id.errors import StreamExpiredError, UserInputError
from track_id.models import Match, SampleHit
from track_id.scan import (
    build_tracklist,
    collapse_tracks,
    identify_moment,
    sample_timestamp,
    track_key,
)

URL = "https://soundcloud.com/artist/some-set"


def _hit(at: float, artist: str | None, title: str | None, backend: str = "shazam") -> SampleHit:
    match = None if title is None else Match(artist=artist, title=title, album="Album")
    return SampleHit(at=at, match=match, backend=None if match is None else backend)


def test_track_key_normalizes_case_and_punctuation() -> None:
    assert track_key("DJ Shadow", "Midnight!") == track_key("dj shadow", "midnight")
    assert track_key("A", "Song") != track_key("A", "Other")


def test_dedup_collapses_consecutive_matches_only() -> None:
    hits = [
        _hit(0, "Artist", "Song"),
        _hit(150, "ARTIST", "song"),
        _hit(300, "Other", "Tune"),
        _hit(450, "Artist", "Song"),
        _hit(600, None, None),
        _hit(750, "Artist", "Song"),
    ]
    tracks = collapse_tracks(hits)
    assert [track["at"] for track in tracks] == ["0:00", "5:00", "7:30", "12:30"]
    assert tracks[0]["title"] == "Song"
    assert tracks[0]["backend"] == "shazam"


async def test_chunking_stops_at_the_sample_cap_and_reports_where_to_resume() -> None:
    resolved = make_resolved(duration=10_000)

    async def sample(ts: float) -> SampleHit:
        return _hit(ts, "A", f"Song {int(ts)}")

    result = await build_tracklist(
        URL,
        make_settings(max_scan_samples=30),
        end="",
        interval_seconds=10,
        resolved=resolved,
        sample_fn=sample,
    )
    assert result["samples"] == 30
    assert result["interval_seconds"] == 60
    assert result["more_to_scan_from"] == "30:00"
    assert result["stop_reason"] == "sample_limit"
    assert result["scanned"] == "0:00 – 29:00"
    assert result["tracks"][-1]["at"] == "29:00"


async def test_scan_finishes_when_the_range_fits_in_one_chunk() -> None:
    resolved = make_resolved(duration=200)

    async def sample(ts: float) -> SampleHit:
        return _hit(ts, "A", "Song")

    result = await build_tracklist(
        URL,
        make_settings(),
        start="0",
        end="3:00",
        interval_seconds=60,
        resolved=resolved,
        sample_fn=sample,
    )
    assert result["samples"] == 3
    assert result["more_to_scan_from"] is None
    assert result["stop_reason"] == "complete"
    assert result["unmatched_samples"] == 0
    assert len(result["tracks"]) == 1
    assert result["tracks"][0]["at"] == "0:00"


async def test_time_budget_returns_a_resume_timestamp() -> None:
    resolved = make_resolved(duration=10_000)
    clock = {"now": 0.0}

    async def sample(ts: float) -> SampleHit:
        clock["now"] += 10
        await asyncio.sleep(0)
        return _hit(ts, "A", f"Song {int(ts)}")

    result = await build_tracklist(
        URL,
        make_settings(max_scan_samples=30, scan_concurrency=2, scan_time_budget_seconds=15),
        interval_seconds=60,
        resolved=resolved,
        sample_fn=sample,
        monotonic=lambda: clock["now"],
    )
    assert result["samples"] == 2
    assert result["stop_reason"] == "time_budget"
    assert result["more_to_scan_from"] == "2:00"
    assert [track["at"] for track in result["tracks"]] == ["0:00", "1:00"]


async def test_results_stay_in_order_when_later_samples_finish_first() -> None:
    resolved = make_resolved(duration=1_000)

    async def sample(ts: float) -> SampleHit:
        await asyncio.sleep(0.05 if ts == 0 else 0.01)
        return _hit(ts, "A", f"Song {int(ts)}")

    result = await build_tracklist(
        URL,
        make_settings(scan_concurrency=3),
        end="6:40",
        interval_seconds=150,
        resolved=resolved,
        sample_fn=sample,
    )
    assert [track["at"] for track in result["tracks"]] == ["0:00", "2:30", "5:00"]
    assert [track["title"] for track in result["tracks"]] == ["Song 0", "Song 150", "Song 300"]


async def test_failed_sample_counts_as_unmatched_and_does_not_abort_the_scan() -> None:
    resolved = make_resolved(duration=400)

    async def sample(ts: float) -> SampleHit:
        if ts == 0:
            raise RuntimeError("ffmpeg blew up")
        return _hit(ts, "A", "Song")

    result = await build_tracklist(
        URL,
        make_settings(),
        end="4:00",
        interval_seconds=60,
        resolved=resolved,
        sample_fn=sample,
    )
    assert result["samples"] == 4
    assert result["unmatched_samples"] == 1
    assert result["tracks"][0]["at"] == "1:00"


async def test_expired_stream_is_resolved_once_and_retried() -> None:
    resolved = make_resolved(duration=100)
    resolves: list[bool] = []
    opens: list[float] = []

    async def fake_resolve(url: str, settings: object, force: bool = False) -> object:
        resolves.append(force)
        return resolved

    @asynccontextmanager
    async def fake_open(
        _resolved: object, start: float, _length: float, _settings: object
    ) -> AsyncIterator[str]:
        opens.append(start)
        if len(opens) == 1:
            raise StreamExpiredError("403")
        yield "clip.mp3"

    async def fake_recognize(path: str, settings: object) -> tuple[Match, str]:
        assert path == "clip.mp3"
        return Match(artist="A", title="Song"), "shazam"

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("track_id.scan.resolve_track", fake_resolve)
    monkeypatch.setattr("track_id.scan.open_clip", fake_open)
    monkeypatch.setattr("track_id.scan.recognize_file", fake_recognize)
    try:
        hit = await sample_timestamp(URL, 95, make_settings(clip_seconds=12))
    finally:
        monkeypatch.undo()
    assert resolves == [False, True]
    assert opens == [88, 88]
    assert hit.match is not None
    assert hit.match.title == "Song"
    assert hit.backend == "shazam"


async def test_a_second_expired_stream_is_not_retried_forever() -> None:
    async def fake_resolve(url: str, settings: object, force: bool = False) -> object:
        return make_resolved()

    @asynccontextmanager
    async def fake_open(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        raise StreamExpiredError("still expired")
        yield "clip.mp3"

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("track_id.scan.resolve_track", fake_resolve)
    monkeypatch.setattr("track_id.scan.open_clip", fake_open)
    try:
        with pytest.raises(StreamExpiredError):
            await sample_timestamp(URL, 10, make_settings())
    finally:
        monkeypatch.undo()


async def test_identify_retries_later_and_reports_the_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []

    async def fake_resolve(url: str, settings: object, force: bool = False) -> object:
        return make_resolved(duration=300)

    async def fake_sample(url: str, ts: float, settings: object) -> SampleHit:
        seen.append(ts)
        if ts == 0:
            return SampleHit(at=ts, match=None, backend=None)
        return SampleHit(at=ts, match=Match(artist="A", title="Song"), backend="audd")

    monkeypatch.setattr("track_id.scan.resolve_track", fake_resolve)
    monkeypatch.setattr("track_id.scan.sample_timestamp", fake_sample)
    result = await identify_moment(URL, "0", make_settings(retry_offset_seconds=20))
    assert seen == [0, 20]
    assert result["offset_seconds"] == 20
    assert result["at"] == "0:20"
    assert result["backend"] == "audd"
    assert result["match"]["title"] == "Song"  # type: ignore[index]


async def test_identify_can_disable_the_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    async def fake_resolve(url: str, settings: object, force: bool = False) -> object:
        return make_resolved(duration=300)

    async def fake_sample(url: str, ts: float, settings: object) -> SampleHit:
        seen.append(ts)
        return SampleHit(at=ts, match=None, backend=None)

    monkeypatch.setattr("track_id.scan.resolve_track", fake_resolve)
    monkeypatch.setattr("track_id.scan.sample_timestamp", fake_sample)
    result = await identify_moment(URL, "1:00", make_settings(retry_offset_seconds=0))
    assert seen == [60]
    assert result["match"] is None
    assert "30" in str(result["hint"]) or "later" in str(result["hint"])


async def test_identify_shifts_the_clip_near_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    starts: list[tuple[float, float]] = []

    async def fake_resolve(url: str, settings: object, force: bool = False) -> object:
        return make_resolved(duration=100)

    @asynccontextmanager
    async def fake_open(
        _resolved: object, start: float, length: float, _settings: object
    ) -> AsyncIterator[str]:
        starts.append((start, length))
        yield "clip.mp3"

    async def fake_recognize(path: str, settings: object) -> tuple[Match, str]:
        return Match(artist="A", title="Song"), "shazam"

    monkeypatch.setattr("track_id.scan.resolve_track", fake_resolve)
    monkeypatch.setattr("track_id.scan.open_clip", fake_open)
    monkeypatch.setattr("track_id.scan.recognize_file", fake_recognize)
    result = await identify_moment(
        URL, "1:35", make_settings(clip_seconds=12, retry_offset_seconds=20)
    )
    assert starts == [(88, 12)]
    assert result["at"] == "1:35"
    assert result["offset_seconds"] == 0


async def test_timestamp_past_the_end_is_a_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(url: str, settings: object, force: bool = False) -> object:
        return make_resolved(duration=100)

    monkeypatch.setattr("track_id.scan.resolve_track", fake_resolve)
    with pytest.raises(UserInputError, match="past the end"):
        await identify_moment(URL, "2:00", make_settings())
