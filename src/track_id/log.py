"""One JSON line per log event, with secrets and signed URLs stripped."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

_SECRETS: list[str] = []
_URL_RE = re.compile(r"https?://[^\s\"'\\<>]+")


def set_redactions(secrets: list[str | None]) -> None:
    """Remember secrets that must never appear in a log line. Empty values are ignored."""

    global _SECRETS
    _SECRETS = [secret for secret in secrets if secret and len(secret) >= 4]


def redact_text(text: str) -> str:
    """Drop query strings and replace configured secrets.

    SoundCloud page URLs keep their path so logs can say which track was asked
    for. Every other URL, including signed media URLs, keeps only its host.
    """

    def replace_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        trimmed = raw.rstrip(".,);]")
        suffix = raw[len(trimmed) :]
        try:
            parts = urlsplit(trimmed)
        except ValueError:
            return "[url]" + suffix
        host = parts.hostname or "host"
        if host == "soundcloud.com" or host.endswith(".soundcloud.com"):
            return f"{parts.scheme}://{host}{parts.path}" + suffix
        # A URL with no path is a host (and maybe a port), not a signed media URL.
        # Uvicorn logs its own bind address this way.
        if parts.path in ("", "/"):
            port = f":{parts.port}" if parts.port else ""
            return f"{parts.scheme}://{host}{port}{parts.path}" + suffix
        return f"{parts.scheme}://{host}/[redacted]" + suffix

    cleaned = _URL_RE.sub(replace_url, text)
    for secret in _SECRETS:
        if secret in cleaned:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


class RedactingJsonFormatter(logging.Formatter):
    """Format a log record as a single JSON object and then redact it."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            payload["event"] = event
        if record.exc_info and record.exc_info[1] is not None:
            payload["error_type"] = type(record.exc_info[1]).__name__
        line = json.dumps(payload, ensure_ascii=False, default=str)
        return redact_text(line)


def configure_logging(level: str, secrets: list[str | None]) -> None:
    """Send every library log through the redacting JSON formatter."""

    set_redactions(secrets)
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingJsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def uvicorn_log_config(level: str) -> dict[str, Any]:
    """Uvicorn log config that uses the same redacting formatter.

    Uvicorn's default access log prints the request path, which contains
    `MCP_PATH_SECRET`. Replacing the formatter is what keeps that off the logs.
    `MCPServer.run` does not accept a log config, so the process starts uvicorn
    itself with this dict.
    """

    # dictConfig rejects lowercase names such as "info". uvicorn.run's own
    # log_level argument is the one that wants the lowercase form.
    normalized = level.upper()
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": "track_id.log.RedactingJsonFormatter"}},
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "stream": "ext://sys.stdout",
            }
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": normalized, "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": normalized, "propagate": False},
            "uvicorn.access": {"handlers": ["default"], "level": normalized, "propagate": False},
        },
        "root": {"handlers": ["default"], "level": normalized},
    }


def log_event(logger: logging.Logger, message: str, **event: Any) -> None:
    logger.info(message, extra={"event": event})
