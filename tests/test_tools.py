"""Tool contracts, error shaping, and the HTTP endpoint."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import Client

from tests.conftest import make_resolved, make_settings
from track_id.models import Match, SampleHit
from track_id.server import (
    INSTRUCTIONS,
    activate,
    build_server,
    get_set_info,
    identify_track,
    make_http_app,
    scan_tracklist,
)

URL = "https://soundcloud.com/artist/some-set"


def _payload(result: object) -> dict:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None)
    if content:
        import json

        return json.loads(content[0].text)
    assert isinstance(result, dict)
    return result


async def test_get_set_info_returns_metadata_and_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    activate(settings)

    async def fake_resolve(url: str, settings: object, force: bool = False) -> object:
        return make_resolved(duration=3750)

    monkeypatch.setattr("track_id.server.resolve_track", fake_resolve)
    result = await get_set_info(URL)
    assert result["title"] == "Closing Set"
    assert result["reply"] == (
        "**[Closing Set](https://soundcloud.com/artist/some-set)**\nDJ Example · 1:02:30"
    )
    assert "Artist - Song" not in result["reply"]
    assert result["uploader"] == "DJ Example"
    assert result["duration"] == "1:02:30"
    assert result["duration_seconds"] == 3750
    assert result["permalink"] == URL
    assert "Artist - Song" in result["description"]
    assert result["preview_only"] is False


async def test_bad_url_and_bad_timestamp_are_error_objects_not_exceptions() -> None:
    activate(make_settings())
    bad_url = await get_set_info("https://evil.example/track")
    assert "error" in bad_url
    assert "soundcloud.com" in bad_url["error"]
    bad_time = await identify_track(URL, "nope")
    assert "error" in bad_time
    assert "Traceback" not in bad_time["error"]


async def test_unexpected_failures_hide_the_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    activate(make_settings())

    async def fake_resolve(url: str, settings: object, force: bool = False) -> object:
        raise RuntimeError("secret stream https://cdn.example/a?sig=1")

    monkeypatch.setattr("track_id.server.resolve_track", fake_resolve)
    result = await get_set_info(URL)
    assert result["error"] == (
        "Something went wrong while handling that request. Please try again."
    )
    assert "sig" not in result["error"]


async def test_identify_and_scan_tools_return_the_service_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activate(make_settings())

    async def fake_resolve(url: str, settings: object, force: bool = False) -> object:
        return make_resolved(duration=600)

    async def fake_sample(url: str, ts: float, settings: object) -> SampleHit:
        return SampleHit(at=ts, match=Match(artist="A", title="Song"), backend="shazam")

    async def fake_scan(*_args: object, **_kwargs: object) -> dict:
        return {
            "set": "Closing Set",
            "scanned": "0:00 – 2:30",
            "samples": 2,
            "unmatched_samples": 0,
            "more_to_scan_from": "5:00",
            "stop_reason": "sample_limit",
            "interval_seconds": 150,
            "tracks": [{"at": "0:00", "artist": "A", "title": "Song", "backend": "shazam"}],
        }

    monkeypatch.setattr("track_id.scan.resolve_track", fake_resolve)
    monkeypatch.setattr("track_id.scan.sample_timestamp", fake_sample)
    monkeypatch.setattr("track_id.server.build_tracklist", fake_scan)
    identified = await identify_track(URL, "0:30")
    assert identified["backend"] == "shazam"
    assert identified["match"]["title"] == "Song"
    assert identified["reply"] == "**Song**\nA · 0:30"
    scanned = await scan_tracklist(URL, "0", "1:00:00", 90)
    assert scanned["more_to_scan_from"] == "5:00"
    assert scanned["tracks"][0]["title"] == "Song"


async def test_in_process_client_lists_tools_with_useful_descriptions() -> None:
    server = build_server(make_settings())
    async with Client(server) as client:
        assert client.instructions is not None
        assert "get_set_info" in client.instructions
        assert "more_to_scan_from" in client.instructions
        listed = await client.list_tools()
        by_name = {tool.name: tool.description or "" for tool in listed.tools}
    assert sorted(by_name) == ["get_set_info", "identify_track", "scan_tracklist"]
    assert "description" in by_name["get_set_info"].lower()
    assert "tracklist" in by_name["get_set_info"].lower()
    assert "more_to_scan_from" in by_name["scan_tracklist"]
    assert "42:10" in by_name["identify_track"]
    assert "reply" in by_name["identify_track"]
    assert "reply" in by_name["scan_tracklist"]
    assert "reply" in by_name["get_set_info"]
    assert "Paste `reply` exactly" in INSTRUCTIONS
    assert "check the description" in INSTRUCTIONS.lower() or "description" in INSTRUCTIONS.lower()


async def test_client_call_returns_an_error_object_for_a_bad_url() -> None:
    server = build_server(make_settings())
    async with Client(server) as client:
        result = await client.call_tool(
            "get_set_info", {"url": "https://soundcloud.com.evil.com/a/b"}
        )
    payload = _payload(result)
    assert "error" in payload
    assert result.is_error is False


async def test_healthz_and_secret_path_over_http() -> None:
    settings = make_settings()
    app = make_http_app(settings)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as http:
            response = None
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    response = await http.get(f"{base}/healthz")
                except httpx.HTTPError:
                    await _nap()
                    continue
                if response.status_code == 200:
                    break
                await _nap()
            assert response is not None
            assert response.status_code == 200
            assert response.text == "ok"
            hidden = await http.get(f"{base}/mcp")
            assert hidden.status_code == 404
        async with Client(f"{base}{settings.mcp_path}") as mcp_client:
            listed = await mcp_client.list_tools()
            names = sorted(tool.name for tool in listed.tools)
            result = await mcp_client.call_tool(
                "identify_track",
                {"url": "http://example.com/nope", "timestamp": "-1"},
            )
        assert names == ["get_set_info", "identify_track", "scan_tracklist"]
        assert "error" in _payload(result)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


async def _nap() -> None:
    import asyncio

    await asyncio.sleep(0.05)
