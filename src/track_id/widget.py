"""The card Claude renders beside a tool result.

The HTML speaks the MCP Apps postMessage protocol itself, so the sandbox does
not need a third-party script. Cover hosts are listed for the sandbox image
policy; anything else is omitted rather than blocked at the card.
"""

from __future__ import annotations

from pathlib import Path

CARD_URI = "ui://soundcloud-track-id/card"
CARD_HTML = Path(__file__).with_name("card.html").read_text(encoding="utf-8")


def card_csp_domains() -> list[str]:
    """Origins that actually host the artwork these recognizers return."""

    return [
        "https://*.mzstatic.com",
        "https://*.scdn.co",
        "https://*.dzcdn.net",
        "https://*.sndcdn.com",
        "https://*.ytimg.com",
    ]
