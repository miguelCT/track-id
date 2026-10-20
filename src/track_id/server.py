"""MCP server: three tools, a secret path, and a public health check.

The official SDK renamed FastMCP to MCPServer in mcp 2.x. `MCPServer.run` does
not accept a logging config, and uvicorn's access log would print the secret
path, so startup builds the Streamable HTTP app with the same options `run`
would use and serves it with a redacting log config.

DNS rebinding protection is disabled on purpose. Cloud Run's proxy sets Host to
the public service name, which is not known at startup, and Claude's calls may
have no Origin or an Origin of https://claude.ai. The unguessable path is the
access control. Binding 0.0.0.0 already skips the SDK's localhost auto-enable;
the setting is passed explicitly so that stays obvious.

`json_response=True` keeps each call to a single request/response. That fits
Cloud Run better than a long-lived SSE stream, and stateless mode means any
instance can serve any request.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from track_id import __version__
from track_id.audio import resolve_track, soundcloud_path
from track_id.config import Settings, load_settings
from track_id.errors import AudioError, ConfigError, UserInputError
from track_id.log import configure_logging, log_event, uvicorn_log_config
from track_id.present import present_set
from track_id.scan import build_tracklist, identify_moment
from track_id.timestamps import format_timestamp

logger = logging.getLogger(__name__)

INSTRUCTIONS = """
This server identifies music inside a SoundCloud track or a DJ mix uploaded as one audio file.

How to answer the user:
- Paste `reply` exactly. Do not wrap it in a code block and do not add a preamble.
- `reply` is markdown: the cover image when one was found, the song title linked
  to Spotify (otherwise YouTube, otherwise a Google search), then the artist and
  time on one line. A scan is one block per song.
- Do not add album, label, release date, backend, or sample counts unless the
  user asks. Those stay in the other fields.
- If the result has `error`, reply with that sentence only.
- If the user asked for the tracklist written in the description, show
  `description` in full. That text is the answer, not extra metadata.

Workflow:
1. Call get_set_info first and read the description. Many sets already contain a
   tracklist, which is cheaper and more accurate than fingerprinting audio.
   Prefer that tracklist when it is there.
2. Call identify_track for one moment ("what is playing at 42:10").
3. Call scan_tracklist to build a tracklist. Each call samples only a chunk.
   When more_to_scan_from is set, call scan_tracklist again with start set to
   that value until more_to_scan_from is null. Paste each chunk's `reply` in order.
4. A null match is normal. Unreleased tracks, edits, bootlegs, and heavy blends
   often do not fingerprint. Try a timestamp 30-60 seconds later, or use the
   description.

Only SoundCloud URLs are accepted. Do not send other links.
""".strip()

_settings: Settings | None = None


def activate(settings: Settings) -> None:
    global _settings
    _settings = settings


def get_settings() -> Settings:
    if _settings is None:
        raise RuntimeError("Server settings are not loaded")
    return _settings


def build_server(settings: Settings) -> MCPServer:
    """Register the tools and the health check. Does not bind a port."""

    activate(settings)
    mcp = MCPServer(
        name="soundcloud-track-id",
        title="SoundCloud Track ID",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_request: Request) -> Response:
        return PlainTextResponse("ok")

    mcp.tool()(get_set_info)
    mcp.tool()(identify_track)
    mcp.tool()(scan_tracklist)
    return mcp


def make_http_app(settings: Settings) -> Starlette:
    mcp = build_server(settings)
    # host="0.0.0.0" is what the SDK uses to decide whether to auto-enable
    # DNS-rebinding protection. We also pass an explicit disabled setting
    # because this process sits behind Cloud Run's proxy.
    return mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )


async def get_set_info(url: str) -> dict[str, Any]:
    """Metadata for one SoundCloud track or DJ mix.

    Paste `reply` exactly. It is the title, linked to the set, plus the uploader
    and duration. If the user asked for the tracklist in the description, show
    `description` in full as well.

    Call this before identify_track or scan_tracklist. Read `description` first:
    if it already contains a tracklist, use that and do not spend time on audio
    recognition. Recognition is slower and misses unreleased tracks, edits, and blends.

    `duration` is `h:mm:ss` or `m:ss`. `duration_seconds` is the length reported
    by SoundCloud. `preview_only` means this is a Go+ track and only about 30
    seconds of audio can be read.

    The URL must be https on soundcloud.com, www.soundcloud.com, m.soundcloud.com,
    on.soundcloud.com, or api.soundcloud.com. Other URLs are rejected.
    """

    return await _call("get_set_info", fetch_set_info(url, get_settings()))


async def identify_track(url: str, timestamp: str = "0") -> dict[str, Any]:
    """Identify the song playing at one moment in a SoundCloud track or DJ mix.

    Paste `reply` exactly. It shows the cover when Shazam (or a paid fallback)
    returned one, then the title as the listen link, then the artist and time.
    Leave album, label, release date, and `backend` out unless the user asks.

    `timestamp` accepts "42:10", "1:02:30", "2530", or "2530.5". 0 is the start.
    Negative timestamps and timestamps past the end return an error object.

    The server fingerprints a short clip starting at that timestamp. If the
    moment is close to the end, the clip slides backward so it stays inside the
    track. On no match it tries once more about 20 seconds later (unless that
    retry is disabled). `offset_seconds` is 0 when the first clip matched and
    the retry distance when the second clip matched. `at` is the moment that
    matched, or the moment you asked about when nothing matched.

    `match` is null when nothing was recognized. That is normal for unreleased
    tracks, edits, and heavy blends. `hint` suggests another timestamp. `backend`
    is which recognizer matched (shazam, audd, or acrcloud). `match.link`, already
    used in `reply`, is a Spotify URL when the recognizer has one, otherwise a
    YouTube URL, otherwise a Google search for the artist and title. `match.cover`
    is the artwork URL, when the recognizer sent one.

    Prefer get_set_info when the description already lists tracks.
    """

    return await _call("identify_track", identify_moment(url, timestamp, get_settings()))


async def scan_tracklist(
    url: str,
    start: str = "0",
    end: str = "",
    interval_seconds: int = 150,
) -> dict[str, Any]:
    """Sample a SoundCloud mix and return a deduplicated tracklist for that range.

    Paste `reply` exactly: one markdown block per song, then "Continue from …"
    when this chunk is unfinished. Keep calling while `more_to_scan_from` is set
    and paste the replies in order. Leave sample counts and album metadata out
    unless the user asks.

    Samples the half-open range [start, end). Leave `end` empty to scan until the
    audio ends. `start` and `end` use the same timestamp formats as identify_track.
    `interval_seconds` is how far apart samples are; values below 60 are raised to 60.

    Each call takes at most a server-configured number of samples (default 30).
    A long mix therefore comes back in chunks. If `more_to_scan_from` is a
    timestamp, call scan_tracklist again with start set to that value (and the
    same end) until `more_to_scan_from` is null. The same field is set when the
    per-call time budget runs out, with whatever tracks were already found.
    `stop_reason` is "complete", "sample_limit", or "time_budget".

    Samples inside one call run a few at a time, and `tracks` is always in
    chronological order. Consecutive samples of the same song collapse into one
    entry whose `at` is the first time it was heard. A song that leaves and comes
    back stays as two entries. Each track's `link` is Spotify, then YouTube, then
    a Google search, and is already the link in `reply`. `unmatched_samples` counts
    moments with no fingerprint; that is normal for blends and unreleased tracks.

    Check get_set_info's description for an existing tracklist before scanning.
    """

    return await _call(
        "scan_tracklist",
        build_tracklist(
            url,
            get_settings(),
            start=start,
            end=end,
            interval_seconds=interval_seconds,
        ),
    )


async def fetch_set_info(url: str, settings: Settings) -> dict[str, Any]:
    resolved = await resolve_track(url, settings)
    payload: dict[str, Any] = {
        "title": resolved.title,
        "uploader": resolved.uploader,
        "duration": format_timestamp(resolved.duration_seconds),
        "duration_seconds": resolved.duration_seconds,
        "permalink": resolved.permalink,
        "description": resolved.description,
        "preview_only": resolved.preview_only,
    }
    if resolved.preview_only:
        payload["playable_seconds"] = resolved.playable_seconds
        payload["note"] = (
            "SoundCloud only exposes a short Go+ preview (about 30 seconds). "
            "Timestamps past the preview cannot be identified."
        )
    log_event(
        logger,
        "get_set_info",
        tool="get_set_info",
        soundcloud_path=soundcloud_path(resolved.page_url),
        preview_only=resolved.preview_only,
        duration_seconds=resolved.duration_seconds,
    )
    return present_set(payload)


async def _call(tool: str, awaitable: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    try:
        return await awaitable
    except UserInputError as exc:
        log_event(logger, "tool rejected", tool=tool, reason=str(exc))
        return {"error": str(exc)}
    except AudioError as exc:
        log_event(logger, "tool failed", tool=tool, reason=str(exc))
        return {"error": str(exc)}
    except Exception as exc:
        log_event(logger, "tool crashed", tool=tool, error_type=type(exc).__name__)
        return {"error": "Something went wrong while handling that request. Please try again."}


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    configure_logging(
        settings.log_level,
        [
            settings.mcp_path_secret,
            settings.audd_api_token,
            settings.acr_access_key,
            settings.acr_access_secret,
        ],
    )
    from track_id.recognizers import build_recognizers

    names = [item.name for item in build_recognizers(settings)]
    log_event(
        logger,
        "starting",
        port=settings.port,
        recognizers=names,
        stateless_http=True,
        json_response=True,
        dns_rebinding_protection=False,
    )
    if not names:
        logger.warning("no recognizers are configured")

    import uvicorn

    app = make_http_app(settings)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=True,
        log_config=uvicorn_log_config(settings.log_level),
    )


if __name__ == "__main__":
    main()
