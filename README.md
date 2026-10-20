# SoundCloud Track ID

A small remote [MCP](https://modelcontextprotocol.io) server. Point Claude at a public SoundCloud track or DJ mix and ask what is playing at a timestamp, or ask it to build a tracklist a chunk at a time.

It is meant for one person. There are no accounts. The lock is a long random string in the URL. Anyone who has that URL can use the server, so treat it like a password.

Recognition defaults to Shazam through the free, unofficial [shazamio](https://github.com/shazamio/ShazamIO) library. No API key is required. [AudD](https://audd.io) and [ACRCloud](https://www.acrcloud.com) are optional paid fallbacks.

The same Docker image runs on Cloud Run (scale to zero) and on a VPS or Raspberry Pi.

## How a request works

1. Claude calls `get_set_info` and reads the description. If the uploader already wrote a tracklist, that is faster and more accurate than fingerprinting.
2. For one moment ("what is this at 42:10"), Claude calls `identify_track`.
3. For a whole mix, Claude calls `scan_tracklist`. Each call samples one chunk. If the reply includes `more_to_scan_from`, Claude calls again with `start` set to that value until `more_to_scan_from` is null.

The server never downloads the whole file. [yt-dlp](https://github.com/yt-dlp/yt-dlp) resolves a short-lived stream URL (`download=False`, Python API, not a shell). ffmpeg then writes only the seconds needed: mono, 44.1 kHz, 128 kbps MP3, seeking with `-ss` before `-i`.

SoundCloud serves fMP4 HLS. ffmpeg 6 produces an empty file if it seeks that remote playlist directly, because the init segment is dropped. For `.m3u8` URLs the server downloads just the fragments that overlap the clip (typically a few hundred KB) and seeks inside that short local playlist. A progressive URL is seeked remotely, and yt-dlp's HTTP headers are passed through.

Only `https` URLs on these hosts are accepted: `soundcloud.com`, `www.soundcloud.com`, `m.soundcloud.com`, `on.soundcloud.com`, `api.soundcloud.com`. Usernames in the URL, other ports, IP addresses, and any other host are rejected. The rejected URL is not echoed back.

## Run it on your computer

You need Python 3.12, ffmpeg, and a network path to SoundCloud and Shazam.

```bash
# macOS
brew install ffmpeg

# Debian / Ubuntu / Raspberry Pi OS
sudo apt update && sudo apt install -y ffmpeg python3.12 python3.12-venv
```

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export MCP_PATH_SECRET="$(openssl rand -hex 16)"
export RECOGNIZERS=shazam
python -m track_id
```

`openssl rand -hex 16` prints 32 hex characters, which meets the 24-character minimum. Write that value down. The server listens on `0.0.0.0:8080`.

```bash
curl -s http://127.0.0.1:8080/healthz
# ok

# This path is unguessable. /mcp without the secret is 404.
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/mcp
```

The MCP endpoint is:

```text
http://127.0.0.1:8080/<MCP_PATH_SECRET>/mcp
```

For a quick local experiment you can set `ALLOW_INSECURE_NO_SECRET=1` and omit the secret. The path then becomes `/dev/mcp`. Do not use that anywhere reachable from the internet.

Try one real track (this downloads a few seconds, then asks Shazam):

```bash
PYTHONPATH=src python scripts/smoke_test.py \
  "https://soundcloud.com/artist/track" 0:30
```

## Run it with Docker

The image is Debian-based (`python:3.12-slim`), not Alpine. `shazamio` 0.8.1 depends on `shazamio-core` 1.1.2, which publishes glibc wheels for `linux/amd64` and `linux/arm64` and does not publish a musllinux wheel. Alpine would try to compile Rust. The same image runs on Cloud Run and on a 64-bit Raspberry Pi.

The process runs as uid 10001 (`app`), not root.

```bash
docker build -t soundcloud-track-id .

export MCP_PATH_SECRET="$(openssl rand -hex 16)"
docker run --rm -p 8080:8080 \
  -e MCP_PATH_SECRET="$MCP_PATH_SECRET" \
  -e RECOGNIZERS=shazam \
  soundcloud-track-id
```

```bash
curl -s http://127.0.0.1:8080/healthz
```

## Check it with MCP Inspector

[MCP Inspector](https://github.com/modelcontextprotocol/inspector) lists the tools and can call them. Transport is **Streamable HTTP**.

```bash
npx @modelcontextprotocol/inspector
```

Open the printed local URL and connect to:

```text
http://127.0.0.1:8080/<MCP_PATH_SECRET>/mcp
```

You should see `get_set_info`, `identify_track`, and `scan_tracklist`. A non-interactive check:

```bash
npx @modelcontextprotocol/inspector --cli \
  "http://127.0.0.1:8080/${MCP_PATH_SECRET}/mcp" \
  --method tools/list
```

## Deploy to Cloud Run

Cloud Run's free tier still requires a billing account. Turn on a budget alert before the first deploy so a surprise does not become a bill. In the console: **Billing → Budgets & alerts → Create budget**. A $5 monthly budget on this project, with email at 50% and 90%, is enough for personal use.

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

export MCP_PATH_SECRET="$(openssl rand -hex 16)"
# Save this. You will need it for the connector URL, and Cloud Run will not show it again.

gcloud run deploy soundcloud-track-id \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances 1 \
  --memory 1Gi \
  --cpu 1 \
  --timeout 900 \
  --set-env-vars "MCP_PATH_SECRET=${MCP_PATH_SECRET},RECOGNIZERS=shazam"
```

`--allow-unauthenticated` means Google's IAM check is open. The random path is what keeps strangers out. `--min-instances 0` scales to zero when you are not using it. `--max-instances 1` caps spend. `--timeout 900` is Cloud Run's maximum request time. The scan time budget defaults to 600 seconds so a scan finishes and returns before that limit.

The command prints a service URL like `https://soundcloud-track-id-xxxxx-uc.a.run.app`. The connector URL is:

```text
https://soundcloud-track-id-xxxxx-uc.a.run.app/<MCP_PATH_SECRET>/mcp
```

Health check (no secret):

```bash
curl -s https://soundcloud-track-id-xxxxx-uc.a.run.app/healthz
```

The first request after idle is a cold start: the container has to boot, then yt-dlp and Shazam run. That can take well over ten seconds. Later requests on a warm instance are faster.

## Connect Claude

In Claude on the web or in the mobile app (a plan that includes custom connectors):

1. **Settings → Connectors → Add custom connector**.
2. Name it `SoundCloud Track ID`.
3. Paste the full `https://…/<secret>/mcp` URL.
4. Save. Claude should list the three tools.

Then ask, with a real public track URL:

> What song is playing at 0:30 in https://soundcloud.com/artist/track ?

Do not paste the connector URL into chats, screenshots, or logs. If it leaks, generate a new secret and redeploy:

```bash
export MCP_PATH_SECRET="$(openssl rand -hex 16)"
gcloud run services update soundcloud-track-id \
  --region us-central1 \
  --update-env-vars "MCP_PATH_SECRET=${MCP_PATH_SECRET}"
```

## Keep the secret in Secret Manager

Optional. Env vars are fine for a personal service. Secret Manager keeps the value out of the deploy command line.

```bash
printf '%s' "$MCP_PATH_SECRET" | gcloud secrets create mcp-path-secret --data-file=-

PROJECT_NUMBER="$(gcloud projects describe YOUR_PROJECT_ID --format='value(projectNumber)')"
gcloud secrets add-iam-policy-binding mcp-path-secret \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud run deploy soundcloud-track-id \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --min-instances 0 \
  --max-instances 1 \
  --memory 1Gi \
  --cpu 1 \
  --timeout 900 \
  --set-secrets "MCP_PATH_SECRET=mcp-path-secret:latest" \
  --set-env-vars "RECOGNIZERS=shazam"
```

## Deploy from GitHub (optional)

Cloud Run can build on every push to `main`. In the console: **Cloud Run → your service → Set up continuous deployment**, connect the repo, and set the same region, memory, timeout, instance limits, and env vars as the command above. Prefer Secret Manager for `MCP_PATH_SECRET` so the GitHub workflow never sees it.

A manual workflow, if you would rather push an image yourself:

```yaml
# .github/workflows/deploy.yml
name: deploy
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ vars.WIF_PROVIDER }}
          service_account: ${{ vars.DEPLOY_SA }}
      - uses: google-github-actions/setup-gcloud@v2
      - run: |
          gcloud run deploy soundcloud-track-id \
            --source . \
            --region us-central1 \
            --allow-unauthenticated \
            --min-instances 0 \
            --max-instances 1 \
            --memory 1Gi \
            --timeout 900
```

Leave `MCP_PATH_SECRET` on the existing service (or in Secret Manager) so a deploy does not rotate it by accident.

## Self-host on a VPS or Raspberry Pi

Same image. On the Pi, build for arm64 (the default if you build on the Pi itself):

```bash
docker build -t soundcloud-track-id .
docker run -d --name track-id --restart unless-stopped -p 127.0.0.1:8080:8080 \
  -e MCP_PATH_SECRET="$MCP_PATH_SECRET" \
  -e RECOGNIZERS=shazam \
  soundcloud-track-id
```

Bind to `127.0.0.1` and put HTTPS in front. Claude's connector requires a public `https://` URL.

Caddy on the same machine:

```caddy
track-id.example.com {
  reverse_proxy 127.0.0.1:8080
}
```

Or a Cloudflare Tunnel, which does not need an open port:

```bash
cloudflared tunnel --url http://127.0.0.1:8080
```

A quick tunnel prints a public `https://….trycloudflare.com` hostname. The secret path is still required. A named tunnel is better if you want a stable hostname.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8080` | Listen port. Cloud Run sets this. |
| `MCP_PATH_SECRET` | required | 24–128 letters, digits, `_`, `-`. The URL path. |
| `ALLOW_INSECURE_NO_SECRET` | off | `1` allows a missing secret (path `/dev/mcp`) for local use. |
| `RECOGNIZERS` | `shazam` | Comma-separated: `shazam`, `audd`, `acrcloud`. First match wins. |
| `AUDD_API_TOKEN` | unset | Enables AudD when `audd` is in the chain. |
| `ACR_HOST` | unset | ACRCloud identify host, for example `identify-eu-west-1.acrcloud.com`. |
| `ACR_ACCESS_KEY` | unset | Set all three ACR variables or none. |
| `ACR_ACCESS_SECRET` | unset | Used for the HMAC-SHA1 signature. |
| `CLIP_SECONDS` | `12` | Length of each fingerprint clip (1–60). |
| `RETRY_OFFSET_SECONDS` | `20` | On no match, try this many seconds later. `0` disables it. |
| `MAX_SCAN_SAMPLES` | `30` | Samples per `scan_tracklist` call (1–100). |
| `SCAN_CONCURRENCY` | `3` | Samples in flight at once (1–8). |
| `SCAN_TIME_BUDGET_SECONDS` | `600` | Stop a scan and return partial results (1–840). |
| `STREAM_URL_TTL_SECONDS` | `300` | How long a resolved stream URL is reused. |
| `FFMPEG_TIMEOUT_SECONDS` | `90` | Kill a stuck ffmpeg (5–300). |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |

Invalid values abort startup. A partial ACRCloud config (host set, key missing) aborts too.

Paid fallbacks, still with Shazam first:

```bash
export RECOGNIZERS=shazam,audd,acrcloud
export AUDD_API_TOKEN=...
export ACR_HOST=identify-eu-west-1.acrcloud.com
export ACR_ACCESS_KEY=...
export ACR_ACCESS_SECRET=...
```

AudD or ACRCloud is skipped when its credentials are missing, even if the name is listed. If the chain ends up empty, recognition calls fail with a clear error.

## The three tools

`get_set_info(url)` returns `title`, `uploader`, `duration` (`m:ss` or `h:mm:ss`), `duration_seconds`, `permalink`, `description` (truncated at 4000 characters), and `preview_only`. Go+ tracks also include `playable_seconds` and a `note`. Only about 30 seconds of a Go+ preview can be read.

`identify_track(url, timestamp="0")` accepts `42:10`, `1:02:30`, `2530`, or `2530.5`. It returns `set`, `at`, `match` (or null), `backend`, and `offset_seconds`. A null match is a normal result, not a tool error. `hint` suggests another timestamp. If the moment is within one clip of the end, the clip slides backward so it stays inside the audio. `match.link` is a Spotify URL when the recognizer has one, otherwise YouTube, otherwise a Google search for the artist and title. The same order is used for each track in a scan.

`scan_tracklist(url, start="0", end="", interval_seconds=150)` samples the half-open range `[start, end)`. An empty `end` means "through the playable audio". Intervals below 60 seconds are raised to 60. The reply includes `tracks` (chronological, consecutive duplicates collapsed), `samples`, `unmatched_samples`, `scanned`, `interval_seconds`, `stop_reason` (`complete`, `sample_limit`, or `time_budget`), and `more_to_scan_from` (the next timestamp to pass as `start`, or null).

Bad URLs, bad timestamps, private or removed tracks, and geo blocks return `{"error": "..."}` rather than a protocol-level failure. One failed sample inside a scan becomes an unmatched sample; it does not abort the chunk.

## Limitations

- Unreleased tracks, DJ edits, bootlegs, and heavy blends often do not fingerprint. Try 30–60 seconds later, or use the description.
- SoundCloud Go+ tracks expose a short preview (about 30 seconds) even when the metadata duration is the full song. Timestamps past that preview cannot be identified.
- shazamio talks to Shazam's unofficial endpoints. It can break without notice, and automated use may conflict with Shazam's terms. This project is for personal use.
- Downloading or fingerprinting audio may conflict with SoundCloud's terms if you go beyond personal use. Do not point this at other people's private tracks or run it as a public service.
- A datacenter IP (Cloud Run, many VPSes) is more likely to see HTTP 403 or 429 from SoundCloud than a home connection is.
- Playlists are rejected. Pass one track URL.
- There is no account system. The secret path is the only access control.

## Troubleshooting

**`yt-dlp` stopped resolving SoundCloud.** SoundCloud changes their site. The error text tells you to update yt-dlp. Bump the pin and redeploy:

```bash
pip install -U yt-dlp
pip index versions yt-dlp   # note the version you just installed
```

Put that exact version in `requirements.txt` and in `pyproject.toml`, then redeploy. Do not float the dependency; a surprise bump is how a working server breaks.

**Shazam stopped matching everything.** Same idea for shazamio:

```bash
pip install -U shazamio
```

Copy the new `shazamio` version (and the `shazamio-core` version it pins) into `requirements.txt` and `pyproject.toml`. If upstream drops the manylinux wheel, the Docker build will try to compile Rust and fail; stay on Debian and check the wheel tags before merging the bump. Until it is fixed, set `RECOGNIZERS=audd` or `acrcloud` if you have a key.

**Cold start feels hung.** With `--min-instances 0` the first call boots the container. Claude's client timeout can fire before that plus a scan finishes. Ask for one timestamp first (`identify_track`), then scan.

**Cloud Run kills a long scan.** The platform limit is 900 seconds. `SCAN_TIME_BUDGET_SECONDS` defaults to 600 and cannot be set above 840, so the handler returns a partial tracklist with `stop_reason: "time_budget"` and `more_to_scan_from` instead of getting cut off. Keep the budget under the Cloud Run timeout.

**403 or "stream expired".** Signed media URLs die quickly. The server caches them for `STREAM_URL_TTL_SECONDS` (default 300) and, on a 403, resolves once more and retries the clip. A second failure is returned as an error. A 403 on every resolve usually means SoundCloud is blocking the server IP; run the container from a residential or home connection, or try again later. HTTP 429 means the same thing with a rate limit.

**Logs.** Each line is one JSON object. The secret path, AudD token, and ACRCloud key are replaced with `[redacted]`. SoundCloud page URLs keep their path and drop the query string. Every other URL, including the signed media URL, keeps only the host. Do not turn on a proxy that logs full request paths if that log store is shared.

## Tests

No network is required.

```bash
pip install -r requirements-dev.txt
ruff check src tests scripts
pytest
```

## Implementation notes

These differ from an earlier sketch of the API. Behavior follows the installed libraries.

- **`mcp` 2.3.0 renamed `FastMCP` to `MCPServer`** (`from mcp.server.mcpserver import MCPServer`). `mcp.server.fastmcp` raises `ModuleNotFoundError`. The server is not pinned to mcp 1.x.
- **Uvicorn is started by this process**, not by `MCPServer.run`. `run` cannot take a log config, and uvicorn's access log would print `/{secret}/mcp`. A redacting JSON formatter is installed for the access log and the app log.
- **`stateless_http=True` and `json_response=True`.** Cloud Run does not pin a client to one instance, and a single JSON body fits its request model better than a long-lived SSE stream.
- **DNS rebinding protection is off.** The SDK only auto-enables it for localhost binds. This process binds `0.0.0.0` behind Cloud Run, whose `Host` is the public service name and whose client `Origin` may be missing or `https://claude.ai`. `TransportSecuritySettings(enable_dns_rebinding_protection=False)` is passed explicitly. The secret path is the access control.
- **`www.soundcloud.com` is allowed**, along with the apex, `m.`, `on.`, and `api.` hosts.
- **HLS clips are trimmed before ffmpeg.** `-ss` before `-i` on SoundCloud's remote fMP4 playlist yields a few hundred bytes and a successful exit. The overlapping segments are fetched with httpx (private and loopback hosts rejected), written to a temp playlist, and ffmpeg seeks that file with `-ss` still before `-i`. The temp files are deleted when the clip is done. A clip smaller than a few kilobytes is treated as a failure instead of being sent to Shazam.
- **Shazam's HTTP client is replaced.** shazamio's default client retries many times with no connect timeout. This server uses httpx with a 10 second connect timeout and a 60 second total timeout, and also wraps the call in `asyncio.wait_for(..., 60)`.
- **The Shazam signature is 12 seconds, not the library default of 10.** shazamio-core fingerprints one slice from the middle of the clip. On real mix clips, 10 seconds often returned no match while the same files matched at 12. Sending 16 seconds or more of a blend made the match disappear again.
- **Extra response fields** that are safe to ignore: `preview_only`, `playable_seconds`, `note`, `offset_seconds`, `offsets_tried`, `stop_reason`, `interval_seconds`. The fields the tools were specified to return are all present.
- **A sample that throws is counted unmatched** and the scan continues. A failure while resolving the URL up front still returns `{"error": "..."}`.
- **Layout.** Code lives in `src/track_id/`: `config.py`, `timestamps.py`, `audio.py`, `cache.py`, `scan.py`, `server.py`, `log.py`, `errors.py`, `models.py`, and `recognizers/` (`shazam.py`, `audd.py`, `acrcloud.py`). Tests are offline and use respx for the paid HTTP APIs.

Pinned runtime versions: `httpx==0.28.1`, `mcp==2.3.0`, `shazamio==0.8.1`, `yt-dlp==2026.8.19`. Dev: `pytest==9.1.1`, `pytest-asyncio==1.4.0`, `respx==0.23.1`, `ruff==0.17.1`.
