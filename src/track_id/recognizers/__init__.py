"""Recognizer protocol, registry, and ordered fallback chain."""

from __future__ import annotations

import logging
from typing import Protocol

from track_id.config import Settings
from track_id.errors import AudioError
from track_id.models import Match
from track_id.recognizers.acrcloud import ACRCloudRecognizer
from track_id.recognizers.audd import AudDRecognizer
from track_id.recognizers.shazam import ShazamRecognizer

logger = logging.getLogger(__name__)


class Recognizer(Protocol):
    name: str

    async def recognize(self, path: str) -> Match | None: ...

    def is_configured(self) -> bool: ...


def build_recognizers(settings: Settings) -> list[Recognizer]:
    """Build the ordered chain, skipping backends that are not configured."""

    factories = {
        "shazam": lambda: ShazamRecognizer(),
        "audd": lambda: AudDRecognizer(settings),
        "acrcloud": lambda: ACRCloudRecognizer(settings),
    }
    active: list[Recognizer] = []
    seen: set[str] = set()
    for name in settings.recognizers:
        if name in seen:
            continue
        seen.add(name)
        recognizer = factories[name]()
        if recognizer.is_configured():
            active.append(recognizer)
        else:
            logger.warning(
                "recognizer skipped",
                extra={"event": {"recognizer": name, "reason": "not configured"}},
            )
    return active


async def run_chain(recognizers: list[Recognizer], path: str) -> tuple[Match | None, str | None]:
    """Return the first match. A backend that raises is logged and skipped."""

    for recognizer in recognizers:
        try:
            match = await recognizer.recognize(path)
        except Exception as exc:
            logger.warning(
                "recognizer failed",
                extra={"event": {"recognizer": recognizer.name, "error_type": type(exc).__name__}},
            )
            continue
        if match is not None and (match.title or "").strip():
            return match, recognizer.name
    return None, None


async def recognize_file(path: str, settings: Settings) -> tuple[Match | None, str | None]:
    recognizers = build_recognizers(settings)
    if not recognizers:
        raise AudioError(
            "No recognition backend is configured. shazam needs no key; "
            "AudD needs AUDD_API_TOKEN and ACRCloud needs ACR_HOST, ACR_ACCESS_KEY, "
            "and ACR_ACCESS_SECRET."
        )
    return await run_chain(recognizers, path)


__all__ = [
    "ACRCloudRecognizer",
    "AudDRecognizer",
    "Recognizer",
    "ShazamRecognizer",
    "build_recognizers",
    "recognize_file",
    "run_chain",
]
