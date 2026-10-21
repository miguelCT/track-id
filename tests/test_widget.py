"""The recognition result is also an MCP Apps card."""

from __future__ import annotations

from mcp import Client

from tests.conftest import make_settings
from track_id.server import build_server
from track_id.widget import CARD_URI

URL = "https://soundcloud.com/artist/some-set"


async def test_tools_point_at_one_card_and_the_server_advertises_it() -> None:
    server = build_server(make_settings())
    async with Client(server) as client:
        extensions = client.server_capabilities.extensions or {}
        listed = await client.list_tools()
        resource = await client.read_resource(CARD_URI)
    assert extensions["io.modelcontextprotocol/ui"]["mimeTypes"] == ["text/html;profile=mcp-app"]
    metas = []
    for tool in listed.tools:
        dumped = tool.model_dump(by_alias=True)
        metas.append(dumped.get("_meta") or dumped.get("meta") or {})
    assert len(metas) == 3
    assert all(meta["ui"]["resourceUri"] == CARD_URI for meta in metas)
    content = resource.contents[0]
    assert content.mime_type == "text/html;profile=mcp-app"
    assert "ui/initialize" in content.text
    assert "innerHTML" not in content.text
    meta = content.meta or {}
    domains = meta["ui"]["csp"]["resourceDomains"]
    assert "https://*.mzstatic.com" in domains
    assert meta["ui"]["prefersBorder"] is False
