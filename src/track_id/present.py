"""Markdown the chat client can paste as the whole answer.

Claude does not render a custom card. It does render a markdown image, a linked
title, and one following line. `reply` is that message. Album, label, release
date, and scan stats stay on the tool payload for when the user asks.
"""

from __future__ import annotations

from typing import Any

from track_id.recognizers.http import https_image


def present_identify(payload: dict[str, Any]) -> dict[str, Any]:
    shown = dict(payload)
    shown["reply"] = _identify_reply(payload)
    return shown


def present_scan(payload: dict[str, Any]) -> dict[str, Any]:
    shown = dict(payload)
    shown["reply"] = _scan_reply(payload)
    return shown


def present_set(payload: dict[str, Any]) -> dict[str, Any]:
    shown = dict(payload)
    title = _text(payload.get("title")) or "Untitled"
    uploader = _text(payload.get("uploader"))
    duration = _text(payload.get("duration"))
    bits = [text for text in (uploader, duration) if text]
    if payload.get("preview_only"):
        bits.append("preview only")
    lines = [_linked_title(title, _text(payload.get("permalink")))]
    if bits:
        lines.append(" · ".join(bits))
    shown["reply"] = "\n".join(lines)
    return shown


def song_card(
    *,
    title: object,
    artist: object = None,
    at: object = None,
    link: object = None,
    cover: object = None,
) -> str:
    """Cover, linked title, then artist and time on one line."""

    name = _text(title) or "Unknown song"
    lines: list[str] = []
    image = https_image(cover)
    if image:
        lines.append(f"![{_image_alt(name)}]({image})")
        lines.append("")
    lines.append(_linked_title(name, _text(link)))
    meta = [text for text in (_text(artist), _text(at)) if text]
    if meta:
        lines.append(" · ".join(meta))
    return "\n".join(lines)


def _identify_reply(payload: dict[str, Any]) -> str:
    match = payload.get("match")
    at = _text(payload.get("at"))
    if not isinstance(match, dict) or not _text(match.get("title")):
        return f"{at} — no match" if at else "No match"
    return song_card(
        title=match.get("title"),
        artist=match.get("artist"),
        at=at,
        link=match.get("link"),
        cover=match.get("cover"),
    )


def _scan_reply(payload: dict[str, Any]) -> str:
    blocks: list[str] = []
    tracks = payload.get("tracks")
    if isinstance(tracks, list):
        for track in tracks:
            if not isinstance(track, dict) or not _text(track.get("title")):
                continue
            blocks.append(
                song_card(
                    title=track.get("title"),
                    artist=track.get("artist"),
                    at=track.get("at"),
                    link=track.get("link"),
                    cover=track.get("cover"),
                )
            )
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


def _linked_title(title: str, link: str | None) -> str:
    label = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("*", "\\*")
    if link and link.startswith("https://") and ")" not in link:
        return f"**[{label}]({link})**"
    return f"**{label}**"


def _image_alt(title: str) -> str:
    return title.replace("[", "").replace("]", "").replace("\n", " ")


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()
