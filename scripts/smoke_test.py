"""Resolve a public SoundCloud URL, cut one clip, and optionally recognize it.

This hits the network. It is not part of the pytest suite.

Examples:
    PYTHONPATH=src python scripts/smoke_test.py \\
        "https://soundcloud.com/artist/track" 0:30

    PYTHONPATH=src python scripts/smoke_test.py URL 1:02 --out /tmp/clip.mp3 --no-recognize
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from track_id.audio import (  # noqa: E402
    clip_window,
    extract_clip_to,
    resolve_track,
    soundcloud_path,
)
from track_id.config import load_settings  # noqa: E402
from track_id.timestamps import format_timestamp, parse_timestamp  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clip (and optionally recognize) one SoundCloud moment."
    )
    parser.add_argument("url", help="Public https://soundcloud.com/... URL")
    parser.add_argument(
        "timestamp", nargs="?", default="0:30", help='For example "42:10" or "90"'
    )
    parser.add_argument(
        "--out",
        default="clip.mp3",
        help="Where to write the MP3 so you can listen to it",
    )
    parser.add_argument(
        "--no-recognize",
        action="store_true",
        help="Only resolve and clip; do not call a recognition backend",
    )
    return parser


async def _run() -> int:
    args = _parser().parse_args()
    if not os.environ.get("MCP_PATH_SECRET") and not os.environ.get("ALLOW_INSECURE_NO_SECRET"):
        os.environ["ALLOW_INSECURE_NO_SECRET"] = "1"
    settings = load_settings()
    timestamp = parse_timestamp(args.timestamp)

    started = time.perf_counter()
    resolved = await resolve_track(args.url, settings, force=True)
    resolve_s = time.perf_counter() - started
    host = urlsplit(resolved.stream_url).hostname
    print(f"title:    {resolved.title}")
    print(f"uploader: {resolved.uploader}")
    duration = format_timestamp(resolved.duration_seconds)
    print(f"duration: {duration} ({resolved.duration_seconds:.1f}s)")
    print(f"path:     {soundcloud_path(resolved.page_url)}")
    print(f"stream:   {host} (signed URL not printed)")
    if resolved.preview_only:
        print("note:     Go+ preview only; about 30 seconds of audio are readable")

    start, length = clip_window(timestamp, resolved.playable_seconds, settings.clip_seconds)
    print(f"clip:     {format_timestamp(start)} for {length:.1f}s -> {args.out}")
    clip_started = time.perf_counter()
    await extract_clip_to(resolved, start, length, args.out, settings)
    clip_s = time.perf_counter() - clip_started
    size = await asyncio.to_thread(os.path.getsize, args.out)
    print(f"wrote:    {args.out} ({size} bytes)")

    recognize_s = 0.0
    if not args.no_recognize:
        from track_id.recognizers import recognize_file

        recognize_started = time.perf_counter()
        match, backend = await recognize_file(args.out, settings)
        recognize_s = time.perf_counter() - recognize_started
        if match is None:
            print("match:    none")
        else:
            print(f"match:    {match.artist} — {match.title} [{backend}]")
            if match.album:
                print(f"album:    {match.album}")
            if match.link:
                print(f"link:     {match.link}")

    print(
        f"timings:  resolve {resolve_s:.2f}s, clip {clip_s:.2f}s, recognize {recognize_s:.2f}s"
    )
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_run()))
    except Exception as exc:  # noqa: BLE001 - CLI should print a clean message
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
