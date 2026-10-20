"""Result types shared by audio extraction, recognition, and scan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Match:
    """A song identified in a clip. `title` is required for this to count as a match."""

    artist: str | None
    title: str | None
    album: str | None = None
    label: str | None = None
    release_date: str | None = None
    link: str | None = None
    cover: str | None = None
    confidence: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "artist": self.artist,
            "title": self.title,
            "album": self.album,
            "label": self.label,
            "release_date": self.release_date,
            "link": self.link,
            "cover": self.cover,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ResolvedTrack:
    """Metadata plus a direct audio URL. The stream URL is short-lived."""

    page_url: str
    title: str
    uploader: str
    duration_seconds: float
    permalink: str
    description: str
    stream_url: str
    http_headers: dict[str, str]
    preview_only: bool
    playable_seconds: float
    resolved_at: float


@dataclass(frozen=True)
class SampleHit:
    """One scan or identify sample, in timeline order."""

    at: float
    match: Match | None
    backend: str | None
    error: str | None = None
