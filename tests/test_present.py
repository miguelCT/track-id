"""Concise summaries and collapsed detail blocks."""

from __future__ import annotations

from track_id.present import collapsed, present_identify, present_scan, present_set


def test_identify_summary_is_the_song_and_the_link() -> None:
    shown = present_identify(
        {
            "set": "Mix Series 52",
            "at": "23:50",
            "match": {
                "artist": "Ilogik",
                "title": "I Just Can't Stop",
                "album": "Bang Up for It 2",
                "label": "Riot Recordings",
                "release_date": "2011",
                "link": "https://open.spotify.com/track/abc",
                "confidence": None,
            },
            "backend": "shazam",
            "offset_seconds": 0,
        }
    )
    assert shown["summary"] == (
        "23:50 — Ilogik — I Just Can't Stop\nhttps://open.spotify.com/track/abc"
    )
    assert shown["details"].startswith("<details>")
    assert "<summary>Details</summary>" in shown["details"]
    assert "Bang Up for It 2" in shown["details"]
    assert "Riot Recordings" in shown["details"]
    assert "2011" in shown["details"]
    assert "shazam" in shown["details"]
    assert "spotify.com" not in shown["details"]
    assert shown["match"]["title"] == "I Just Can't Stop"


def test_identify_details_escape_html_and_note_a_later_match() -> None:
    shown = present_identify(
        {
            "set": "A & B",
            "at": "1:00",
            "match": {
                "artist": "A & B",
                "title": "Song",
                "album": "<b>LP",
                "label": None,
                "release_date": None,
                "link": "https://www.youtube.com/watch?v=abc",
                "confidence": 0.5,
            },
            "backend": "audd",
            "offset_seconds": 20,
        }
    )
    assert shown["summary"].startswith("1:00 — A & B — Song")
    assert "<b>" not in shown["details"]
    assert "&lt;b&gt;LP" in shown["details"]
    assert "A &amp; B" in shown["details"]
    assert "Matched 20s after" in shown["details"]
    assert "0.5" in shown["details"]


def test_no_match_summary_stays_one_line() -> None:
    shown = present_identify(
        {
            "set": "Closing Set",
            "at": "45:40",
            "match": None,
            "backend": None,
            "offset_seconds": None,
            "hint": "No match at this moment. Try around 46:25.",
        }
    )
    assert shown["summary"] == "45:40 — no match"
    assert "Try around 46:25" in shown["details"]
    assert "Album" not in shown["details"]


def test_scan_summary_lists_songs_and_where_to_continue() -> None:
    shown = present_scan(
        {
            "set": "Closing Set",
            "scanned": "0:00 – 2:30",
            "samples": 2,
            "unmatched_samples": 1,
            "more_to_scan_from": "5:00",
            "stop_reason": "sample_limit",
            "interval_seconds": 150,
            "tracks": [
                {
                    "at": "0:00",
                    "artist": "A",
                    "title": "Song",
                    "album": "LP",
                    "label": None,
                    "release_date": "2011",
                    "link": "https://open.spotify.com/track/1",
                    "backend": "shazam",
                },
                {
                    "at": "2:30",
                    "artist": None,
                    "title": "Tune",
                    "album": None,
                    "label": None,
                    "release_date": None,
                    "link": "https://www.google.com/search?q=Tune",
                    "backend": "shazam",
                },
            ],
        }
    )
    assert shown["summary"] == (
        "0:00 — A — Song\n"
        "https://open.spotify.com/track/1\n"
        "\n"
        "2:30 — Tune\n"
        "https://www.google.com/search?q=Tune\n"
        "\n"
        "Continue from 5:00."
    )
    assert "album LP" in shown["details"]
    assert "released 2011" in shown["details"]
    assert "2:30 — Tune" not in shown["details"]
    assert "2 (1 with no match)" in shown["details"]
    assert "Interval: 2:30" in shown["details"]
    assert "sample limit" in shown["details"]
    assert shown["more_to_scan_from"] == "5:00"


def test_scan_with_nothing_recognized() -> None:
    empty = present_scan(
        {
            "set": "Closing Set",
            "scanned": "0:00 – 4:00",
            "samples": 4,
            "unmatched_samples": 4,
            "more_to_scan_from": None,
            "stop_reason": "complete",
            "interval_seconds": 60,
            "tracks": [],
        }
    )
    assert empty["summary"] == "No matches."
    partial = present_scan({**empty, "more_to_scan_from": "5:00", "tracks": []})
    assert partial["summary"] == "No matches in this chunk.\n\nContinue from 5:00."


def test_set_summary_is_title_uploader_and_duration() -> None:
    shown = present_set(
        {
            "title": "Closing Set",
            "uploader": "DJ Example",
            "duration": "1:02:30",
            "permalink": "https://soundcloud.com/artist/some-set",
            "description": "01. Artist - Song\n02. Other - Tune",
            "preview_only": False,
        }
    )
    assert shown["summary"] == "Closing Set — DJ Example — 1:02:30"
    assert "<pre>01. Artist - Song\n02. Other - Tune</pre>" in shown["details"]
    assert shown["description"].startswith("01. Artist")

    preview = present_set(
        {
            "title": "Go+ Track",
            "uploader": "Someone",
            "duration": "3:10",
            "permalink": "https://soundcloud.com/a/b",
            "description": "",
            "preview_only": True,
            "playable_seconds": 30,
            "note": "Only a short preview.",
        }
    )
    assert preview["summary"] == "Go+ Track — Someone — 3:10 (preview only)"
    assert "Playable audio: 0:30" in preview["details"]
    assert "Only a short preview." in preview["details"]
    assert "<pre>" not in preview["details"]


def test_collapsed_block_is_omitted_when_there_is_nothing_to_hide() -> None:
    assert collapsed([]) == ""
    assert collapsed([("Album", None), ("Label", "  ")]) == ""
