"""Optional ACRCloud backend. Used only when host, access key, and secret are set.

The signature string is `POST\\n/v1/identify\\n{access_key}\\naudio\\n1\\n{timestamp}`,
HMAC-SHA1 with the access secret, then base64. `signature_version` is 1.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from pathlib import Path
from typing import Any

from track_id.config import Settings
from track_id.errors import RecognizerError
from track_id.models import Match
from track_id.recognizers.http import clean, post_multipart


class ACRCloudRecognizer:
    name = "acrcloud"

    def __init__(self, settings: Settings) -> None:
        self._host = settings.acr_host
        self._access_key = settings.acr_access_key
        self._access_secret = settings.acr_access_secret

    def is_configured(self) -> bool:
        return bool(self._host and self._access_key and self._access_secret)

    async def recognize(self, path: str) -> Match | None:
        if not self.is_configured():
            raise RecognizerError("ACRCloud is not configured")
        assert self._host and self._access_key and self._access_secret
        audio = await asyncio.to_thread(Path(path).read_bytes)
        timestamp = str(int(time.time()))
        signature = build_signature(self._access_key, self._access_secret, timestamp)
        payload = await post_multipart(
            f"https://{self._host}/v1/identify",
            data={
                "access_key": self._access_key,
                "data_type": "audio",
                "signature_version": "1",
                "signature": signature,
                "sample_bytes": str(len(audio)),
                "timestamp": timestamp,
            },
            files={"sample": ("clip.mp3", audio, "audio/mpeg")},
        )
        return parse_acrcloud(payload)


def build_signature(access_key: str, access_secret: str, timestamp: str) -> str:
    message = f"POST\n/v1/identify\n{access_key}\naudio\n1\n{timestamp}"
    digest = hmac.new(
        access_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_acrcloud(payload: dict[str, Any]) -> Match | None:
    """`status.code` 0 is a match, 1001 is no result, anything else is an error."""

    status = payload.get("status")
    if not isinstance(status, dict):
        raise RecognizerError("ACRCloud response had no status")
    code = status.get("code")
    if code == 1001:
        return None
    if code != 0:
        raise RecognizerError(f"ACRCloud error {code}")
    metadata = payload.get("metadata")
    music = metadata.get("music") if isinstance(metadata, dict) else None
    if not isinstance(music, list) or not music or not isinstance(music[0], dict):
        return None
    song = music[0]
    title = clean(song.get("title"))
    if title is None:
        return None
    return Match(
        artist=_artists(song),
        title=title,
        album=_album(song),
        label=clean(song.get("label")),
        release_date=clean(song.get("release_date")),
        link=_spotify_link(song),
        confidence=_score(song.get("score")),
    )


def _artists(song: dict[str, Any]) -> str | None:
    raw = song.get("artists")
    if not isinstance(raw, list):
        return None
    names: list[str] = []
    for artist in raw:
        if not isinstance(artist, dict):
            continue
        name = clean(artist.get("name"))
        if name:
            names.append(name)
    return ", ".join(names) or None


def _album(song: dict[str, Any]) -> str | None:
    album = song.get("album")
    if not isinstance(album, dict):
        return None
    return clean(album.get("name"))


def _spotify_link(song: dict[str, Any]) -> str | None:
    external = song.get("external_metadata")
    if not isinstance(external, dict):
        return None
    spotify = external.get("spotify")
    if not isinstance(spotify, dict):
        return None
    track = spotify.get("track")
    if not isinstance(track, dict):
        return None
    track_id = clean(track.get("id"))
    if track_id is None:
        return None
    return f"https://open.spotify.com/track/{track_id}"


def _score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
