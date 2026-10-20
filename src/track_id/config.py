"""Load and validate every setting from the environment. One place, fail fast."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from os import environ
from urllib.parse import urlsplit

from track_id.errors import ConfigError

_SECRET_STRICT = re.compile(r"^[A-Za-z0-9_-]{24,128}$")
_SECRET_LOOSE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_KNOWN_RECOGNIZERS = ("shazam", "audd", "acrcloud")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    port: int
    mcp_path_secret: str
    allow_insecure_no_secret: bool
    recognizers: tuple[str, ...]
    clip_seconds: float
    retry_offset_seconds: float
    max_scan_samples: int
    scan_concurrency: int
    scan_time_budget_seconds: float
    stream_url_ttl_seconds: float
    ffmpeg_timeout_seconds: float
    log_level: str
    audd_api_token: str | None
    acr_host: str | None
    acr_access_key: str | None
    acr_access_secret: str | None

    def __post_init__(self) -> None:
        _check_range("PORT", self.port, 1, 65535, integer=True)
        if self.allow_insecure_no_secret:
            if not _SECRET_LOOSE.fullmatch(self.mcp_path_secret):
                raise ConfigError(
                    "MCP_PATH_SECRET, when set, may contain only letters, digits, '_' and '-'."
                )
        elif not _SECRET_STRICT.fullmatch(self.mcp_path_secret):
            raise ConfigError(
                "MCP_PATH_SECRET is required and must be 24–128 characters of letters, "
                "digits, '_' or '-' (try `openssl rand -hex 16`). "
                "Set ALLOW_INSECURE_NO_SECRET=1 only for local development."
            )
        if not self.recognizers:
            raise ConfigError("RECOGNIZERS is empty. Use shazam, audd, and/or acrcloud.")
        unknown = [name for name in self.recognizers if name not in _KNOWN_RECOGNIZERS]
        if unknown:
            raise ConfigError(
                f"Unknown RECOGNIZERS entry {unknown[0]!r}. Known values: shazam, audd, acrcloud."
            )
        _check_range("CLIP_SECONDS", self.clip_seconds, 1, 60)
        _check_range("RETRY_OFFSET_SECONDS", self.retry_offset_seconds, 0, 180)
        _check_range("MAX_SCAN_SAMPLES", self.max_scan_samples, 1, 100, integer=True)
        _check_range("SCAN_CONCURRENCY", self.scan_concurrency, 1, 8, integer=True)
        # Stay under Cloud Run's 900s request timeout with room for the response itself.
        _check_range("SCAN_TIME_BUDGET_SECONDS", self.scan_time_budget_seconds, 1, 840)
        _check_range("STREAM_URL_TTL_SECONDS", self.stream_url_ttl_seconds, 1, 3600)
        _check_range("FFMPEG_TIMEOUT_SECONDS", self.ffmpeg_timeout_seconds, 5, 300)
        if self.log_level not in _LOG_LEVELS:
            raise ConfigError(f"LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}.")
        acr_parts = (self.acr_host, self.acr_access_key, self.acr_access_secret)
        if any(part is not None for part in acr_parts) and not all(
            part is not None for part in acr_parts
        ):
            raise ConfigError(
                "Set all of ACR_HOST, ACR_ACCESS_KEY, and ACR_ACCESS_SECRET, or none of them."
            )
        if self.acr_host is not None and (
            "/" in self.acr_host or " " in self.acr_host or "@" in self.acr_host
        ):
            raise ConfigError(
                "ACR_HOST must be a bare hostname, for example identify-eu-west-1.acrcloud.com."
            )

    @property
    def mcp_path(self) -> str:
        return f"/{self.mcp_path_secret}/mcp"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read settings from `env`, or from `os.environ` when `env` is omitted."""

    source: Mapping[str, str] = environ if env is None else env
    allow_insecure = _parse_bool(source, "ALLOW_INSECURE_NO_SECRET", default=False)
    secret = _get(source, "MCP_PATH_SECRET")
    if secret is None:
        if not allow_insecure:
            raise ConfigError(
                "MCP_PATH_SECRET is not set. Generate one with `openssl rand -hex 16` "
                "and pass it as an environment variable. For local development only, "
                "set ALLOW_INSECURE_NO_SECRET=1."
            )
        secret = "dev"
    return Settings(
        port=_parse_int(source, "PORT", 8080, 1, 65535),
        mcp_path_secret=secret,
        allow_insecure_no_secret=allow_insecure,
        recognizers=_parse_recognizers(source),
        clip_seconds=_parse_float(source, "CLIP_SECONDS", 12, 1, 60),
        retry_offset_seconds=_parse_float(source, "RETRY_OFFSET_SECONDS", 20, 0, 180),
        max_scan_samples=_parse_int(source, "MAX_SCAN_SAMPLES", 30, 1, 100),
        scan_concurrency=_parse_int(source, "SCAN_CONCURRENCY", 3, 1, 8),
        scan_time_budget_seconds=_parse_float(source, "SCAN_TIME_BUDGET_SECONDS", 600, 1, 840),
        stream_url_ttl_seconds=_parse_float(source, "STREAM_URL_TTL_SECONDS", 300, 1, 3600),
        ffmpeg_timeout_seconds=_parse_float(source, "FFMPEG_TIMEOUT_SECONDS", 90, 5, 300),
        log_level=_parse_log_level(source),
        audd_api_token=_get(source, "AUDD_API_TOKEN"),
        acr_host=_normalize_acr_host(_get(source, "ACR_HOST")),
        acr_access_key=_get(source, "ACR_ACCESS_KEY"),
        acr_access_secret=_get(source, "ACR_ACCESS_SECRET"),
    )


def _get(env: Mapping[str, str], name: str) -> str | None:
    raw = env.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _parse_bool(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = _get(env, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name} must be 1 or 0 (got {raw!r}).")


def _parse_int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer (got {raw!r}).") from exc
    _check_range(name, value, minimum, maximum, integer=True)
    return value


def _parse_float(
    env: Mapping[str, str], name: str, default: float, minimum: float, maximum: float
) -> float:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number (got {raw!r}).") from exc
    _check_range(name, value, minimum, maximum)
    return value


def _parse_log_level(env: Mapping[str, str]) -> str:
    raw = _get(env, "LOG_LEVEL")
    if raw is None:
        return "INFO"
    level = raw.upper()
    if level not in _LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)} (got {raw!r}).")
    return level


def _parse_recognizers(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = _get(env, "RECOGNIZERS") or "shazam"
    names = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    if not names:
        raise ConfigError("RECOGNIZERS is empty. Use shazam, audd, and/or acrcloud.")
    return names


def _normalize_acr_host(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw
    if "://" in value:
        parts = urlsplit(value)
        value = parts.netloc or parts.path
    value = value.strip().strip("/")
    if "/" in value:
        value = value.split("/", 1)[0]
    return value or None


def _check_range(
    name: str,
    value: float,
    minimum: float,
    maximum: float,
    *,
    integer: bool = False,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ConfigError(f"{name} must be a finite number.")
    if integer and int(value) != value:
        raise ConfigError(f"{name} must be an integer.")
    if value < minimum or value > maximum:
        raise ConfigError(f"{name} must be between {minimum:g} and {maximum:g} (got {value:g}).")
