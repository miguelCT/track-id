"""SoundCloud URL allowlist, including the usual SSRF disguises."""

import pytest

from track_id.audio import validate_soundcloud_url
from track_id.errors import UserInputError

ACCEPTED = [
    "https://soundcloud.com/artist/track",
    "https://SOUNDCLOUD.com/Artist/Track",
    "https://m.soundcloud.com/artist/track",
    "https://on.soundcloud.com/AbCdEf",
    "https://api.soundcloud.com/tracks/12345",
    "https://www.soundcloud.com/artist/track",
    "https://soundcloud.com/artist/track?si=abc",
    "https://soundcloud.com/artist/track#t=1:00",
    "https://soundcloud.com:443/artist/track",
    "https://soundcloud.com/artist/sets/some-mix",
    "  https://soundcloud.com/artist/track  ",
]

REJECTED = [
    "http://soundcloud.com/artist/track",
    "https://soundcloud.com.evil.com/artist/track",
    "https://user:pass@soundcloud.com/artist/track",
    "https://soundcloud.com@evil.com/x",
    "https://evil.com@soundcloud.com/x",
    "https://user@soundcloud.com/artist/track",
    "https://127.0.0.1/foo",
    "https://[::1]/foo",
    "https://2130706433/a",
    "https://0x7f000001/a",
    "https://soundcloud.com%2eevil.com/x",
    "https://soundcloud.com%00.evil.com/x",
    "https://soundcloud.com%40evil.com/x",
    "https://evil.com/soundcloud.com/artist/track",
    "https://notsoundcloud.com/artist/track",
    "https://m.soundcloud.com.evil.com/artist/track",
    "https://api.soundcloud.com.evil.com/tracks/1",
    "ftp://soundcloud.com/artist/track",
    "file:///etc/passwd",
    "javascript:https://soundcloud.com/artist/track",
    "https://soundcloud.com",
    "https://soundcloud.com/",
    "https://soundcloud.com:444/artist/track",
    "https://soundcloud.com./artist/track",
    "https://soundcloud.com%00",
    "soundcloud.com/artist/track",
    "https://soundcloud.com/artist/track extra",
    "https://soundcloud.com/a\nb",
    "",
    "   ",
]


@pytest.mark.parametrize("url", ACCEPTED)
def test_accepts_soundcloud_urls(url: str) -> None:
    cleaned = validate_soundcloud_url(url)
    assert cleaned.startswith("https://")
    assert " " not in cleaned


@pytest.mark.parametrize("url", REJECTED)
def test_rejects_everything_else(url: str) -> None:
    with pytest.raises(UserInputError):
        validate_soundcloud_url(url)


def test_query_string_is_preserved_for_ytdlp() -> None:
    url = "https://soundcloud.com/artist/track?si=abc&utm_source=clipboard"
    assert validate_soundcloud_url(url) == url


def test_non_strings_are_rejected() -> None:
    with pytest.raises(UserInputError):
        validate_soundcloud_url(None)  # type: ignore[arg-type]
    with pytest.raises(UserInputError):
        validate_soundcloud_url(["https://soundcloud.com/artist/track"])  # type: ignore[arg-type]
