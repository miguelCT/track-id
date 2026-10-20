"""Short answers for the chat, with the rest behind a collapsed section.

Clients are told to show `summary` and paste `details` unchanged. `details` is an
HTML `<details>` block, which Claude and ChatGPT render as an expandable section.
"""

from __future__ import annotations

from html import escape
from typing import Any

from track_id.timestamps import format_timestamp

_STOP_REASON = {
    "complete": "Finished this range",
    "sample_limit": "Stopped at the sample limit",
    "time_budget": "Stopped at the time limit",
}


def present_identify(payload: dict[str, Any]) -> dict[str, Any]:
    shown = dict(payload)
    shown["summary"] = _identify_summary(payload)
    shown["details"] = _identify_details(payload)
    return shown


def present_scan(payload: dict[str, Any]) -> dict[str, Any]:
    shown = dict(payload)
    shown["summary"] = _scan_summary(payload)
    shown["details"] = _scan_details(payload)
    return shown


def present_set(payload: dict[str, Any]) -> dict[str, Any]:
    shown = dict(payload)
    parts = [str(payload.get("title") or "Untitled")]
    uploader = _text(payload.get("uploader"))
    duration = _text(payload.get("duration"))
    if uploader:
        parts.append(uploader)
    if duration:
        parts.append(duration)
    summary = " — ".join(parts)
    if payload.get("preview_only"):
        summary += " (preview only)"
    shown["summary"] = summary
    bullets: list[tuple[str, object]] = [("Permalink", payload.get("permalink"))]
    playable = payload.get("playable_seconds")
    if payload.get("preview_only") and isinstance(playable, int | float):
        bullets.append(("Playable audio", format_timestamp(float(playable))))
    if payload.get("note"):
        bullets.append(("Note", payload.get("note")))
    shown["details"] = collapsed(bullets, description=payload.get("description"))
    return shown


def collapsed(
    bullets: list[tuple[str, object]],
    *,
    description: object = None,
    extra_lines: list[str] | None = None,
) -> str:
    """An HTML details block. Empty when there is nothing to hide."""

    rows: list[str] = []
    for label, value in bullets:
        text = _text(value)
        if text:
            rows.append(f"<li>{escape(label)}: {escape(text)}</li>")
    for line in extra_lines or []:
        text = line.strip()
        if text:
            rows.append(f"<li>{escape(text)}</li>")
    desc = _text(description)
    if not rows and not desc:
        return ""
    parts = ["<details>", "<summary>Details</summary>"]
    if rows:
        parts.append("<ul>")
        parts.extend(rows)
        parts.append("</ul>")
    if desc:
        parts.append(f"<pre>{escape(desc)}</pre>")
    parts.append("</details>")
    return "\n".join(parts)


def song_line(at: object, artist: object, title: object) -> str:
    name = " — ".join(text for text in (_text(artist), _text(title)) if text)
    when = _text(at)
    if when and name:
        return f"{when} — {name}"
    return name or when or "Unknown song"


def _identify_summary(payload: dict[str, Any]) -> str:
    match = payload.get("match")
    at = _text(payload.get("at"))
    if not isinstance(match, dict) or not _text(match.get("title")):
        return f"{at} — no match" if at else "No match"
    line = song_line(at, match.get("artist"), match.get("title"))
    link = _text(match.get("link"))
    if link:
        return f"{line}\n{link}"
    return line


def _identify_details(payload: dict[str, Any]) -> str:
    match = payload.get("match") if isinstance(payload.get("match"), dict) else {}
    bullets: list[tuple[str, object]] = [
        ("Set", payload.get("set")),
        ("Album", match.get("album")),
        ("Label", match.get("label")),
        ("Released", match.get("release_date")),
        ("Identified by", payload.get("backend")),
    ]
    confidence = match.get("confidence")
    if isinstance(confidence, int | float):
        bullets.append(("Confidence", f"{confidence:g}"))
    offset = payload.get("offset_seconds")
    if isinstance(offset, int | float) and offset > 0:
        bullets.append(("Note", f"Matched {offset:g}s after the time you asked about."))
    if payload.get("preview_only"):
        bullets.append(("Note", "Only a short Go+ preview can be read."))
    if not _text(match.get("title")):
        bullets.append(("Note", payload.get("hint")))
    return collapsed(bullets)


def _scan_summary(payload: dict[str, Any]) -> str:
    blocks: list[str] = []
    tracks = payload.get("tracks")
    if isinstance(tracks, list):
        for track in tracks:
            if not isinstance(track, dict):
                continue
            lines = [song_line(track.get("at"), track.get("artist"), track.get("title"))]
            link = _text(track.get("link"))
            if link:
                lines.append(link)
            blocks.append("\n".join(lines))
    more = _text(payload.get("more_to_scan_from"))
    if blocks:
        text = "\n\n".join(blocks)
    elif more:
        text = "No matches in this chunk."
    else:
        text = "No matches."
    if more:
        text = f"{text}\n\nContinue from {more}."
    return text


def _scan_details(payload: dict[str, Any]) -> str:
    unmatched = payload.get("unmatched_samples")
    samples = payload.get("samples")
    sample_text: object = samples
    if isinstance(samples, int) and isinstance(unmatched, int) and unmatched:
        sample_text = f"{samples} ({unmatched} with no match)"
    interval = payload.get("interval_seconds")
    every: object = None
    if isinstance(interval, int | float):
        every = format_timestamp(float(interval))
    bullets: list[tuple[str, object]] = [
        ("Set", payload.get("set")),
        ("Scanned", payload.get("scanned")),
        ("Samples", sample_text),
        ("Interval", every),
        ("Stopped", _STOP_REASON.get(str(payload.get("stop_reason") or ""))),
    ]
    if payload.get("preview_only"):
        bullets.append(("Note", payload.get("note") or "Only a short Go+ preview can be read."))
    return collapsed(bullets, extra_lines=_track_extras(payload.get("tracks")))


def _track_extras(tracks: object) -> list[str]:
    if not isinstance(tracks, list):
        return []
    lines: list[str] = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        parts: list[str] = []
        album = _text(track.get("album"))
        label = _text(track.get("label"))
        released = _text(track.get("release_date"))
        if album:
            parts.append(f"album {album}")
        if label:
            parts.append(f"label {label}")
        if released:
            parts.append(f"released {released}")
        if not parts:
            continue
        heading = song_line(track.get("at"), track.get("artist"), track.get("title"))
        lines.append(f"{heading}: {'; '.join(parts)}")
    return lines


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()
