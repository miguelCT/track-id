"""Default recognizer: shazamio, which needs no API key.

shazamio's own HTTP client retries up to 20 times and does not set a connect
timeout, so this backend injects an httpx client with a 10s connect timeout and
a 60s total timeout, and also caps the whole call with `asyncio.wait_for`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from track_id.errors import RecognizerError
from track_id.links import prefer_listen_link
from track_id.models import Match
from track_id.recognizers.http import TIMEOUT, clean, https_image

logger = logging.getLogger(__name__)

# shazamio-core fingerprints one slice, taken from the middle when the file is
# longer. Its default is 10 seconds. On the mix clips tried here, that slice
# missed and 12 seconds matched; 16 seconds and longer matched nothing, because
# a blend dilutes the signature. 12 is also what the library's older Python
# signer uses.
SHAZAM_SEGMENT_SECONDS = 12


class HttpxShazamClient:
    """The small slice of shazamio's HTTP client interface that `recognize` uses."""

    async def request(
        self, method: str, url: str, *args: object, **kwargs: object
    ) -> dict[str, Any]:
        del args
        headers = kwargs.get("headers")
        json_body = kwargs.get("json")
        proxy = kwargs.get("proxy")
        client_kwargs: dict[str, Any] = {"timeout": TIMEOUT}
        if isinstance(proxy, str) and proxy:
            client_kwargs["proxy"] = proxy
        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.request(
                    method,
                    url,
                    headers=headers if isinstance(headers, dict) else None,
                    json=json_body,
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.TimeoutException as exc:
            raise RecognizerError("Shazam timed out") from exc
        except httpx.HTTPError as exc:
            raise RecognizerError(f"Shazam request failed ({type(exc).__name__})") from exc
        except ValueError as exc:
            raise RecognizerError("Shazam response was not JSON") from exc
        if not isinstance(payload, dict):
            raise RecognizerError("Shazam response JSON was not an object")
        return payload


class ShazamRecognizer:
    name = "shazam"

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def is_configured(self) -> bool:
        return True

    async def recognize(self, path: str) -> Match | None:
        client = self._client if self._client is not None else _default_client()
        try:
            payload = await asyncio.wait_for(client.recognize(path), timeout=60)
        except TimeoutError as exc:
            raise RecognizerError("Shazam timed out") from exc
        except RecognizerError:
            raise
        except Exception as exc:
            logger.warning(
                "shazam failed",
                extra={"event": {"recognizer": self.name, "error_type": type(exc).__name__}},
            )
            raise RecognizerError(f"Shazam request failed ({type(exc).__name__})") from exc
        if not isinstance(payload, dict):
            raise RecognizerError("Shazam returned an unexpected response")
        return parse_shazam(payload)


def parse_shazam(payload: dict[str, Any]) -> Match | None:
    """Read artist/title from shazamio's `recognize` payload.

    No `track` key means no match. `track.subtitle` is the artist. Album, label,
    and release date live in `track.sections[].metadata[]`.
    """

    track = payload.get("track")
    if not isinstance(track, dict):
        if payload.get("error") or payload.get("errors"):
            raise RecognizerError("Shazam returned an error")
        return None
    title = clean(track.get("title"))
    if title is None:
        return None
    metadata = _section_metadata(track)
    artist = clean(track.get("subtitle"))
    return Match(
        artist=artist,
        title=title,
        album=metadata.get("Album"),
        label=metadata.get("Label"),
        release_date=metadata.get("Released"),
        link=prefer_listen_link(artist=artist, title=title, urls=_provider_urls(track)),
        cover=_cover_url(track),
        confidence=None,
    )


def _cover_url(track: dict[str, Any]) -> str | None:
    """Square artwork from Shazam. The high-resolution cover wins."""

    images = track.get("images")
    if isinstance(images, dict):
        for key in ("coverarthq", "coverart", "background"):
            url = https_image(images.get(key))
            if url:
                return url
    share = track.get("share")
    if isinstance(share, dict):
        return https_image(share.get("image"))
    return None


def _provider_urls(track: dict[str, Any]) -> list[str]:
    """Spotify and YouTube URLs from Shazam's hub. The Shazam page itself is not one."""

    found: list[str] = []
    hub = track.get("hub")
    if isinstance(hub, dict):
        _collect_urls(hub, found)
    sections = track.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if isinstance(section, dict):
                youtube = section.get("youtubeurl")
                if isinstance(youtube, str) and youtube.strip():
                    found.append(youtube.strip())
    return found


def _collect_urls(value: object, found: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"uri", "url"} and isinstance(item, str) and item.strip():
                found.append(item.strip())
            elif isinstance(item, dict | list):
                _collect_urls(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_urls(item, found)


def _section_metadata(track: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    sections = track.get("sections")
    if not isinstance(sections, list):
        return found
    for section in sections:
        if not isinstance(section, dict):
            continue
        items = section.get("metadata")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            key = clean(item.get("title"))
            value = clean(item.get("text"))
            if key and value and key not in found:
                found[key] = value
    return found


def _default_client() -> Any:
    from shazamio import Shazam

    return Shazam(
        http_client=HttpxShazamClient(),
        segment_duration_seconds=SHAZAM_SEGMENT_SECONDS,
    )
