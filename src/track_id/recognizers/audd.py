"""Optional AudD backend. Used only when AUDD_API_TOKEN is set."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from track_id.config import Settings
from track_id.errors import RecognizerError
from track_id.models import Match
from track_id.recognizers.http import clean, post_multipart

AUDD_URL = "https://api.audd.io/"


class AudDRecognizer:
    name = "audd"

    def __init__(self, settings: Settings) -> None:
        self._token = settings.audd_api_token

    def is_configured(self) -> bool:
        return bool(self._token)

    async def recognize(self, path: str) -> Match | None:
        if not self._token:
            raise RecognizerError("AudD is not configured")
        audio = await asyncio.to_thread(Path(path).read_bytes)
        payload = await post_multipart(
            AUDD_URL,
            data={"api_token": self._token, "return": "spotify"},
            files={"file": ("clip.mp3", audio, "audio/mpeg")},
        )
        return parse_audd(payload)


def parse_audd(payload: dict[str, Any]) -> Match | None:
    """`status == success` and `result: null` is a clean no-match, not an error."""

    if payload.get("status") != "success":
        error = payload.get("error")
        detail = ""
        if isinstance(error, dict):
            detail = str(error.get("error_message") or error.get("error_code") or "")
        summary = detail or str(payload.get("status") or "unknown")
        raise RecognizerError(f"AudD error: {summary[:160]}")
    result = payload.get("result")
    if not result:
        return None
    if not isinstance(result, dict):
        raise RecognizerError("AudD result was not an object")
    title = clean(result.get("title"))
    if title is None:
        return None
    return Match(
        artist=clean(result.get("artist")),
        title=title,
        album=clean(result.get("album")),
        label=clean(result.get("label")),
        release_date=clean(result.get("release_date")),
        link=_link(result),
        confidence=None,
    )


def _link(result: dict[str, Any]) -> str | None:
    direct = clean(result.get("song_link"))
    if direct:
        return direct
    spotify = result.get("spotify")
    if not isinstance(spotify, dict):
        return None
    urls = spotify.get("external_urls")
    if not isinstance(urls, dict):
        return None
    return clean(urls.get("spotify"))
