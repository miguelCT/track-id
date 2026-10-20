"""Pick one place to open a match: Spotify, then YouTube, then Google."""

from __future__ import annotations

from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


def prefer_listen_link(
    *,
    artist: str | None,
    title: str | None,
    urls: list[str],
) -> str:
    """Return the best https link for a song that was already identified.

    A Spotify track page wins, then any other Spotify URL (including Shazam's
    search link). A YouTube watch page is next, then a YouTube search. If the
    recognizer named neither service, the link is a Google search for the
    artist and title.
    """

    spotify_track: str | None = None
    spotify_other: str | None = None
    youtube_watch: str | None = None
    youtube_other: str | None = None
    for raw in urls:
        spotify = _spotify(raw)
        if spotify is not None:
            kind, link = spotify
            if kind == "track" and spotify_track is None:
                spotify_track = link
            elif kind != "track" and spotify_other is None:
                spotify_other = link
            continue
        youtube = _youtube(raw)
        if youtube is None:
            continue
        kind, link = youtube
        if kind == "watch" and youtube_watch is None:
            youtube_watch = link
        elif kind != "watch" and youtube_other is None:
            youtube_other = link
    return (
        spotify_track
        or spotify_other
        or youtube_watch
        or youtube_other
        or google_search_link(artist, title)
    )


def google_search_link(artist: str | None, title: str | None) -> str:
    query = " ".join(part.strip() for part in (artist or "", title or "") if part and part.strip())
    return "https://www.google.com/search?" + urlencode({"q": query or "song"})


def _spotify(raw: str) -> tuple[str, str] | None:
    text = raw.strip()
    if text.startswith("spotify:track:"):
        track_id = text.split(":", 2)[2].split("?", 1)[0].strip()
        if track_id:
            return "track", f"https://open.spotify.com/track/{track_id}"
    if text.startswith("spotify:search:"):
        query = unquote(text.split(":", 2)[2]).strip()
        if query:
            return "search", "https://open.spotify.com/search/" + quote(query)
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if host != "open.spotify.com" and not host.endswith(".spotify.com"):
        return None
    path = parts.path or ""
    if path.startswith("/track/"):
        track_id = path.split("/", 2)[2].split("/", 1)[0]
        if track_id:
            return "track", f"https://open.spotify.com/track/{track_id}"
    if path.startswith("/search"):
        query = parse_qs(parts.query).get("q", [""])[0]
        if not query and "/search/" in path:
            query = unquote(path.split("/search/", 1)[1])
        query = query.strip()
        if query:
            return "search", "https://open.spotify.com/search/" + quote(query)
    return None


def _youtube(raw: str) -> tuple[str, str] | None:
    parts = urlsplit(raw.strip())
    host = (parts.hostname or "").lower()
    if host == "youtu.be":
        video_id = parts.path.strip("/").split("/", 1)[0]
        if video_id:
            return "watch", f"https://www.youtube.com/watch?v={video_id}"
    if host not in _YOUTUBE_HOSTS:
        return None
    if parts.path == "/watch":
        video_id = parse_qs(parts.query).get("v", [""])[0].strip()
        if video_id:
            return "watch", f"https://www.youtube.com/watch?v={video_id}"
    if parts.path in {"/results", "/search"}:
        names = ("search_query", "q")
        query = ""
        for name in names:
            found = parse_qs(parts.query).get(name, [""])[0].strip()
            if found:
                query = found
                break
        if query:
            return "search", "https://www.youtube.com/results?" + urlencode({"search_query": query})
    return None
