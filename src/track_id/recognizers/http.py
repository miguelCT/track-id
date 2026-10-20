"""Shared httpx timeout for recognition backends."""

from __future__ import annotations

from typing import Any

import httpx

from track_id.errors import RecognizerError

# Connect 10s, every other phase bounded so the whole call stays under 60s.
TIMEOUT = httpx.Timeout(60.0, connect=10.0)


async def post_multipart(
    url: str,
    *,
    data: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> dict[str, Any]:
    """POST a clip and return a JSON object. Errors carry no response body."""

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(url, data=data, files=files)
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException as exc:
        raise RecognizerError("recognition request timed out") from exc
    except httpx.HTTPError as exc:
        raise RecognizerError(f"recognition request failed ({type(exc).__name__})") from exc
    except ValueError as exc:
        raise RecognizerError("recognition response was not JSON") from exc
    if not isinstance(payload, dict):
        raise RecognizerError("recognition response JSON was not an object")
    return payload


def clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def https_image(value: object) -> str | None:
    """An https image URL that is safe to place inside a markdown image."""

    text = clean(value)
    if text is None or not text.startswith("https://"):
        return None
    if any(char in text for char in " \t\r\n()<>\"'\\"):
        return None
    return text
