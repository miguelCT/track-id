"""Markdown replies: cover, linked title, artist and time."""

from __future__ import annotations

from track_id.present import present_identify, present_scan, present_set, song_card

COVER = "https://is1-ssl.mzstatic.com/image/thumb/cover.jpg"
LINK = "https://open.spotify.com/track/abc"


def test_identify_reply_is_the_cover_title_and_one_line() -> None:
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
                "link": LINK,
                "cover": COVER,
                "confidence": None,
            },
            "backend": "shazam",
            "offset_seconds": 0,
        }
    )
    assert shown["reply"] == (
        f"![I Just Can't Stop]({COVER})\n\n**[I Just Can't Stop]({LINK})**\nIlogik · 23:50"
    )
    assert "Bang Up for It 2" not in shown["reply"]
    assert "shazam" not in shown["reply"]
    assert "details" not in shown
    assert shown["match"]["album"] == "Bang Up for It 2"


def test_reply_escapes_title_characters_and_skips_a_broken_cover() -> None:
    card = song_card(
        title="A * B [Radio Edit]",
        artist="A & B",
        at="1:00",
        link="https://www.youtube.com/watch?v=abc",
        cover="https://cdn.example/a(b).jpg",
    )
    assert card == (
        "**[A \\* B \\[Radio Edit\\]](https://www.youtube.com/watch?v=abc)**\nA & B · 1:00"
    )
    assert "a(b).jpg" not in card


def test_no_match_reply_stays_one_line() -> None:
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
    assert shown["reply"] == "45:40 — no match"
    assert "Try around" not in shown["reply"]
    assert shown["hint"].startswith("No match")


def test_scan_reply_lists_songs_and_where_to_continue() -> None:
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
                    "link": "https://open.spotify.com/track/1",
                    "cover": COVER,
                },
                {
                    "at": "2:30",
                    "artist": None,
                    "title": "Tune",
                    "link": "https://www.google.com/search?q=Tune",
                    "cover": None,
                },
            ],
        }
    )
    assert shown["reply"] == (
        f"![Song]({COVER})\n"
        "\n"
        "**[Song](https://open.spotify.com/track/1)**\n"
        "A · 0:00\n"
        "\n"
        "**[Tune](https://www.google.com/search?q=Tune)**\n"
        "2:30\n"
        "\n"
        "Continue from 5:00."
    )
    assert "album" not in shown["reply"].lower()
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
    assert empty["reply"] == "No matches."
    partial = present_scan({**empty, "more_to_scan_from": "5:00", "tracks": []})
    assert partial["reply"] == "No matches in this chunk.\n\nContinue from 5:00."


def test_set_reply_links_the_title() -> None:
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
    assert shown["reply"] == (
        "**[Closing Set](https://soundcloud.com/artist/some-set)**\nDJ Example · 1:02:30"
    )
    assert "Artist - Song" not in shown["reply"]
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
    assert preview["reply"] == (
        "**[Go+ Track](https://soundcloud.com/a/b)**\nSomeone · 3:10 · preview only"
    )
