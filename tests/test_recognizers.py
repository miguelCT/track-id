"""Recognizer parsing, HTTP calls, and the fallback chain. No live network."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
import respx

from tests.conftest import make_settings
from track_id.errors import AudioError, RecognizerError
from track_id.models import Match
from track_id.recognizers import build_recognizers, recognize_file, run_chain
from track_id.recognizers.acrcloud import (
    ACRCloudRecognizer,
    build_signature,
    parse_acrcloud,
)
from track_id.recognizers.audd import AudDRecognizer, parse_audd
from track_id.recognizers.shazam import (
    SHAZAM_SEGMENT_SECONDS,
    ShazamRecognizer,
    _default_client,
    parse_shazam,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _clip(tmp_path: Path) -> str:
    path = tmp_path / "clip.mp3"
    path.write_bytes(b"not-really-audio")
    return str(path)


class _FakeRecognizer:
    def __init__(self, name: str, result: Match | None | Exception) -> None:
        self.name = name
        self._result = result
        self.calls = 0

    def is_configured(self) -> bool:
        return True

    async def recognize(self, path: str) -> Match | None:
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_shazam_match_fixture() -> None:
    match = parse_shazam(_load("shazam_match.json"))
    assert match is not None
    assert match.artist == "Rick Astley"
    assert match.title == "Never Gonna Give You Up"
    assert match.album == "Whenever You Need Somebody"
    assert match.label == "RCA"
    assert match.release_date == "1987"
    assert match.link is not None and match.link.startswith("https://www.shazam.com/")


def test_shazam_nomatch_and_error_fixtures() -> None:
    assert parse_shazam(_load("shazam_nomatch.json")) is None
    with pytest.raises(RecognizerError):
        parse_shazam(_load("shazam_error.json"))


async def test_shazam_recognizer_uses_the_injected_client() -> None:
    class FakeShazam:
        async def recognize(self, path: str) -> dict:
            assert path.endswith("clip.mp3")
            return _load("shazam_match.json")

    match = await ShazamRecognizer(client=FakeShazam()).recognize("clip.mp3")
    assert match is not None
    assert match.title == "Never Gonna Give You Up"


async def test_shazam_recognizer_wraps_client_failures() -> None:
    class Boom:
        async def recognize(self, path: str) -> dict:
            raise RuntimeError("down")

    with pytest.raises(RecognizerError, match="RuntimeError"):
        await ShazamRecognizer(client=Boom()).recognize("clip.mp3")


def test_shazam_signature_uses_twelve_seconds() -> None:
    client = _default_client()
    assert client.core_recognizer.segment_duration_seconds == SHAZAM_SEGMENT_SECONDS
    assert SHAZAM_SEGMENT_SECONDS == 12


def test_audd_fixtures() -> None:
    match = parse_audd(_load("audd_match.json"))
    assert match is not None
    assert match.artist == "Rick Astley"
    assert match.link == "https://lis.tn/NeverGonnaGiveYouUp"
    assert parse_audd(_load("audd_nomatch.json")) is None
    with pytest.raises(RecognizerError, match="invalid token"):
        parse_audd(_load("audd_error.json"))


def test_audd_uses_spotify_link_when_song_link_is_missing() -> None:
    payload = _load("audd_match.json")
    payload["result"]["song_link"] = ""
    match = parse_audd(payload)
    assert match is not None
    assert match.link == "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"


async def test_audd_posts_the_clip_and_token(tmp_path: Path) -> None:
    seen: dict[str, bytes] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json=_load("audd_nomatch.json"))

    settings = make_settings(audd_api_token="audd-test-token", recognizers=("audd",))
    async with respx.mock:
        respx.post("https://api.audd.io/").mock(side_effect=capture)
        match = await AudDRecognizer(settings).recognize(_clip(tmp_path))
    assert match is None
    body = seen["body"]
    assert b"audd-test-token" in body
    assert b"spotify" in body
    assert b"not-really-audio" in body


async def test_audd_http_error(tmp_path: Path) -> None:
    settings = make_settings(audd_api_token="token")
    async with respx.mock:
        respx.post("https://api.audd.io/").mock(return_value=httpx.Response(500))
        with pytest.raises(RecognizerError):
            await AudDRecognizer(settings).recognize(_clip(tmp_path))


def test_acr_signature_matches_the_documented_string() -> None:
    signature = build_signature("access-key", "access-secret", "1700000000")
    message = b"POST\n/v1/identify\naccess-key\naudio\n1\n1700000000"
    expected = base64.b64encode(
        hmac.new(b"access-secret", message, hashlib.sha1).digest()
    ).decode()
    assert signature == expected


def test_acr_fixtures() -> None:
    match = parse_acrcloud(_load("acrcloud_match.json"))
    assert match is not None
    assert match.artist == "Rick Astley"
    assert match.album == "Whenever You Need Somebody"
    assert match.confidence == 100
    assert match.link == "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
    assert parse_acrcloud(_load("acrcloud_nomatch.json")) is None
    with pytest.raises(RecognizerError, match="3001"):
        parse_acrcloud(_load("acrcloud_error.json"))


async def test_acr_request_is_signed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("track_id.recognizers.acrcloud.time.time", lambda: 1_700_000_000)
    seen: dict[str, bytes] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json=_load("acrcloud_nomatch.json"))

    settings = make_settings(
        recognizers=("acrcloud",),
        acr_host="identify-eu-west-1.acrcloud.com",
        acr_access_key="access-key",
        acr_access_secret="access-secret",
    )
    async with respx.mock:
        respx.post("https://identify-eu-west-1.acrcloud.com/v1/identify").mock(side_effect=capture)
        match = await ACRCloudRecognizer(settings).recognize(_clip(tmp_path))
    assert match is None
    body = seen["body"]
    assert build_signature("access-key", "access-secret", "1700000000").encode() in body
    assert b"access-key" in body
    assert str(len(b"not-really-audio")).encode() in body


def test_chain_skips_unconfigured_backends_in_order() -> None:
    settings = make_settings(
        recognizers=("audd", "shazam", "audd", "acrcloud"),
        audd_api_token=None,
    )
    active = build_recognizers(settings)
    assert [item.name for item in active] == ["shazam"]


def test_chain_includes_paid_backends_only_when_configured() -> None:
    settings = make_settings(
        recognizers=("acrcloud", "audd", "shazam"),
        audd_api_token="token",
        acr_host="identify.example",
        acr_access_key="key",
        acr_access_secret="secret",
    )
    active = build_recognizers(settings)
    assert [item.name for item in active] == ["acrcloud", "audd", "shazam"]


async def test_chain_returns_the_first_match_and_skips_failures() -> None:
    failing = _FakeRecognizer("shazam", RecognizerError("down"))
    empty = _FakeRecognizer("audd", None)
    hit = _FakeRecognizer("acrcloud", Match(artist="A", title="Song"))
    unused = _FakeRecognizer("extra", Match(artist="B", title="Later"))
    match, backend = await run_chain([failing, empty, hit, unused], "clip.mp3")
    assert backend == "acrcloud"
    assert match is not None and match.title == "Song"
    assert unused.calls == 0


async def test_chain_no_match_when_everyone_misses() -> None:
    match, backend = await run_chain(
        [_FakeRecognizer("shazam", None), _FakeRecognizer("audd", RuntimeError("nope"))],
        "clip.mp3",
    )
    assert match is None
    assert backend is None


async def test_recognize_file_errors_when_nothing_is_configured() -> None:
    settings = make_settings(recognizers=("audd", "acrcloud"))
    with pytest.raises(AudioError, match="No recognition backend"):
        await recognize_file("clip.mp3", settings)
