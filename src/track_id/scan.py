"""Identify one moment, or scan a range with a cap, a time budget, and dedup."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable

from track_id.audio import (
    clip_window,
    open_clip,
    resolve_track,
    soundcloud_path,
)
from track_id.config import Settings
from track_id.errors import StreamExpiredError, UserInputError
from track_id.models import Match, ResolvedTrack, SampleHit
from track_id.present import present_identify, present_scan
from track_id.recognizers import recognize_file
from track_id.timestamps import format_timestamp, parse_timestamp

logger = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
SampleFn = Callable[[float], Awaitable[SampleHit]]
Clock = Callable[[], float]


def track_key(artist: str | None, title: str | None) -> str:
    """Case-insensitive key. Punctuation and extra whitespace do not count."""

    def norm(value: str | None) -> str:
        text = _NON_ALNUM.sub(" ", (value or "").casefold())
        return " ".join(text.split())

    return f"{norm(artist)}|{norm(title)}"


def collapse_tracks(hits: list[SampleHit]) -> list[dict[str, object]]:
    """Collapse only consecutive identical matches. A repeat later in the set stays."""

    tracks: list[dict[str, object]] = []
    last_key: str | None = None
    for hit in hits:
        match = hit.match
        if match is None or not (match.title or "").strip():
            last_key = None
            continue
        key = track_key(match.artist, match.title)
        if key == last_key:
            continue
        last_key = key
        tracks.append(
            {
                "at": format_timestamp(hit.at),
                "artist": match.artist,
                "title": match.title,
                "album": match.album,
                "label": match.label,
                "release_date": match.release_date,
                "link": match.link,
                "cover": match.cover,
                "backend": hit.backend,
            }
        )
    return tracks


def ensure_timestamp_playable(timestamp: float, resolved: ResolvedTrack) -> None:
    if timestamp >= resolved.duration_seconds:
        raise UserInputError(
            f"Timestamp {format_timestamp(timestamp)} is past the end of the track "
            f"({format_timestamp(resolved.duration_seconds)})."
        )
    if timestamp >= resolved.playable_seconds and resolved.preview_only:
        raise UserInputError(
            "This looks like a SoundCloud Go+ track: only about 30 seconds of preview "
            "audio can be read, so a timestamp that late cannot be identified."
        )
    if timestamp >= resolved.playable_seconds:
        raise UserInputError(
            f"Timestamp {format_timestamp(timestamp)} is past the playable audio "
            f"({format_timestamp(resolved.playable_seconds)})."
        )


async def sample_timestamp(url: str, timestamp: float, settings: Settings) -> SampleHit:
    """Clip one moment and recognize it. Re-resolve once if the signed URL has expired."""

    resolved = await resolve_track(url, settings)
    if timestamp >= resolved.playable_seconds:
        return SampleHit(at=timestamp, match=None, backend=None, error="past playable audio")
    start, length = clip_window(timestamp, resolved.playable_seconds, settings.clip_seconds)
    try:
        match, backend = await _clip_and_recognize(resolved, start, length, settings, timestamp)
    except StreamExpiredError:
        logger.info(
            "stream expired",
            extra={
                "event": {
                    "soundcloud_path": soundcloud_path(url),
                    "at": format_timestamp(timestamp),
                }
            },
        )
        resolved = await resolve_track(url, settings, force=True)
        match, backend = await _clip_and_recognize(resolved, start, length, settings, timestamp)
    return SampleHit(at=timestamp, match=match, backend=backend)


async def identify_moment(url: str, timestamp: str, settings: Settings) -> dict[str, object]:
    """Recognize the song at `timestamp`, retrying once a bit later on no-match."""

    requested = parse_timestamp(timestamp)
    resolved = await resolve_track(url, settings)
    ensure_timestamp_playable(requested, resolved)
    offsets = [0.0]
    if settings.retry_offset_seconds > 0:
        offsets.append(float(settings.retry_offset_seconds))

    tried: list[float] = []
    seen_starts: set[float] = set()
    for offset in offsets:
        moment = requested + offset
        if moment >= resolved.playable_seconds:
            break
        start, _length = clip_window(moment, resolved.playable_seconds, settings.clip_seconds)
        if round(start, 3) in seen_starts:
            continue
        seen_starts.add(round(start, 3))
        tried.append(offset)
        hit = await sample_timestamp(url, moment, settings)
        if hit.match is not None and (hit.match.title or "").strip():
            payload: dict[str, object] = {
                "set": resolved.title,
                "at": format_timestamp(moment),
                "match": hit.match.as_dict(),
                "backend": hit.backend,
                "offset_seconds": offset,
            }
            if resolved.preview_only:
                payload["preview_only"] = True
            return present_identify(payload)

    payload = {
        "set": resolved.title,
        "at": format_timestamp(requested),
        "match": None,
        "backend": None,
        "offset_seconds": None,
        "offsets_tried": tried,
        "hint": _no_match_hint(requested, resolved, tried, settings),
    }
    if resolved.preview_only:
        payload["preview_only"] = True
    return present_identify(payload)


async def build_tracklist(
    url: str,
    settings: Settings,
    *,
    start: str = "0",
    end: str = "",
    interval_seconds: int = 150,
    resolved: ResolvedTrack | None = None,
    sample_fn: SampleFn | None = None,
    monotonic: Clock | None = None,
) -> dict[str, object]:
    """Sample `[start, end)` until the sample cap or the time budget runs out."""

    if resolved is None:
        resolved = await resolve_track(url, settings)
    start_s = parse_timestamp(start)
    ensure_timestamp_playable(start_s, resolved)
    if end.strip() == "":
        end_s = resolved.playable_seconds
    else:
        end_s = min(parse_timestamp(end), resolved.playable_seconds)
    if start_s >= end_s:
        raise UserInputError(
            f"Nothing to scan between {format_timestamp(start_s)} and {format_timestamp(end_s)}."
        )

    interval = max(60, int(interval_seconds))
    plan: list[float] = []
    cursor = start_s
    while cursor < end_s and len(plan) < settings.max_scan_samples:
        plan.append(cursor)
        cursor += interval

    clock = monotonic or time.monotonic

    async def default_sample(ts: float) -> SampleHit:
        return await sample_timestamp(url, ts, settings)

    started_at = clock()
    hits, started = await _run_samples(
        plan,
        sample_fn or default_sample,
        settings.scan_concurrency,
        deadline=started_at + settings.scan_time_budget_seconds,
        clock=clock,
    )
    completed = [hit for hit in hits[:started] if hit is not None]
    if started < len(plan):
        more: str | None = format_timestamp(plan[started])
        stop_reason = "time_budget"
    elif cursor < end_s:
        more = format_timestamp(cursor)
        stop_reason = "sample_limit"
    else:
        more = None
        stop_reason = "complete"

    payload: dict[str, object] = {
        "set": resolved.title,
        "scanned": _scanned_label(completed),
        "samples": len(completed),
        "unmatched_samples": _unmatched(completed),
        "more_to_scan_from": more,
        "stop_reason": stop_reason,
        "interval_seconds": interval,
        "tracks": collapse_tracks(completed),
    }
    if resolved.preview_only:
        payload["preview_only"] = True
        payload["note"] = (
            "Only a short Go+ preview is available, so the scan cannot cover the full duration."
        )
    logger.info(
        "scan finished",
        extra={
            "event": {
                "soundcloud_path": soundcloud_path(resolved.page_url),
                "samples": len(completed),
                "tracks": len(payload["tracks"]),  # type: ignore[arg-type]
                "stop_reason": stop_reason,
                "more_to_scan_from": more,
                "elapsed_ms": int((clock() - started_at) * 1000),
            }
        },
    )
    return present_scan(payload)


async def _clip_and_recognize(
    resolved: ResolvedTrack,
    start: float,
    length: float,
    settings: Settings,
    timestamp: float,
) -> tuple[Match | None, str | None]:
    clip_started = time.perf_counter()
    async with open_clip(resolved, start, length, settings) as path:
        clip_ms = int((time.perf_counter() - clip_started) * 1000)
        recognize_started = time.perf_counter()
        match, backend = await recognize_file(path, settings)
        recognize_ms = int((time.perf_counter() - recognize_started) * 1000)
    logger.info(
        "sample recognized",
        extra={
            "event": {
                "soundcloud_path": soundcloud_path(resolved.page_url),
                "at": format_timestamp(timestamp),
                "backend": backend,
                "matched": match is not None,
                "clip_ms": clip_ms,
                "recognize_ms": recognize_ms,
            }
        },
    )
    return match, backend


def _no_match_hint(
    timestamp: float,
    resolved: ResolvedTrack,
    tried: list[float],
    settings: Settings,
) -> str:
    causes = "Unreleased tracks, edits, bootlegs, and heavy blends often do not match."
    latest = max(0.0, resolved.playable_seconds - 1)
    suggestion = min(timestamp + 45, latest)
    if any(offset > 0 for offset in tried):
        retried = f" Also tried {settings.retry_offset_seconds:g}s later."
    else:
        retried = ""
    if suggestion > timestamp + 5:
        return (
            f"No match at this moment. Try around {format_timestamp(suggestion)} "
            f"(about 30–60 seconds later). {causes}{retried}"
        )
    return f"No match at this moment. {causes}{retried}"


def _scanned_label(hits: list[SampleHit]) -> str | None:
    if not hits:
        return None
    return f"{format_timestamp(hits[0].at)} – {format_timestamp(hits[-1].at)}"


def _unmatched(hits: list[SampleHit]) -> int:
    return sum(1 for hit in hits if hit.match is None or not (hit.match.title or "").strip())


async def _run_samples(
    plan: list[float],
    sample_fn: SampleFn,
    concurrency: int,
    *,
    deadline: float,
    clock: Clock,
) -> tuple[list[SampleHit | None], int]:
    """Run samples with bounded concurrency. Do not start a new one after `deadline`."""

    results: list[SampleHit | None] = [None] * len(plan)
    started = 0
    pending: set[asyncio.Task[None]] = set()

    async def worker(index: int, ts: float) -> None:
        try:
            results[index] = await sample_fn(ts)
        except Exception as exc:
            logger.warning(
                "sample failed",
                extra={"event": {"at": format_timestamp(ts), "error_type": type(exc).__name__}},
            )
            results[index] = SampleHit(at=ts, match=None, backend=None, error=type(exc).__name__)

    try:
        while started < len(plan) or pending:
            while started < len(plan) and len(pending) < concurrency and clock() < deadline:
                pending.add(asyncio.create_task(worker(started, plan[started])))
                started += 1
            if not pending:
                break
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return results, started
