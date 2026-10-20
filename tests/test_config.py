"""Startup configuration fails fast and otherwise fills defaults."""

import logging.config

import pytest

from track_id.config import load_settings
from track_id.errors import ConfigError
from track_id.log import redact_text, set_redactions, uvicorn_log_config

SECRET = "a" * 32


def test_defaults_when_only_the_secret_is_set() -> None:
    settings = load_settings({"MCP_PATH_SECRET": SECRET})
    assert settings.port == 8080
    assert settings.recognizers == ("shazam",)
    assert settings.clip_seconds == 12
    assert settings.retry_offset_seconds == 20
    assert settings.max_scan_samples == 30
    assert settings.scan_concurrency == 3
    assert settings.scan_time_budget_seconds == 600
    assert settings.stream_url_ttl_seconds == 300
    assert settings.mcp_path == f"/{SECRET}/mcp"


def test_missing_secret_is_rejected() -> None:
    with pytest.raises(ConfigError, match="MCP_PATH_SECRET"):
        load_settings({})


def test_short_secret_is_rejected_unless_local_dev_flag_is_set() -> None:
    with pytest.raises(ConfigError, match="24"):
        load_settings({"MCP_PATH_SECRET": "tooshort"})
    settings = load_settings({"ALLOW_INSECURE_NO_SECRET": "1", "MCP_PATH_SECRET": "local"})
    assert settings.mcp_path_secret == "local"
    insecure = load_settings({"ALLOW_INSECURE_NO_SECRET": "1"})
    assert insecure.mcp_path_secret == "dev"


def test_secret_with_a_slash_is_rejected() -> None:
    with pytest.raises(ConfigError):
        load_settings({"MCP_PATH_SECRET": SECRET + "/mcp"})


@pytest.mark.parametrize(
    "env",
    [
        {"MCP_PATH_SECRET": SECRET, "PORT": "nope"},
        {"MCP_PATH_SECRET": SECRET, "CLIP_SECONDS": "0"},
        {"MCP_PATH_SECRET": SECRET, "SCAN_TIME_BUDGET_SECONDS": "900"},
        {"MCP_PATH_SECRET": SECRET, "MAX_SCAN_SAMPLES": "0"},
        {"MCP_PATH_SECRET": SECRET, "SCAN_CONCURRENCY": "99"},
        {"MCP_PATH_SECRET": SECRET, "RECOGNIZERS": "shazam,spotify"},
        {"MCP_PATH_SECRET": SECRET, "LOG_LEVEL": "verbose"},
        {"MCP_PATH_SECRET": SECRET, "ACR_HOST": "identify.example"},
        {"MCP_PATH_SECRET": SECRET, "RETRY_OFFSET_SECONDS": "-1"},
    ],
)
def test_invalid_values_fail_fast(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_settings(env)


def test_recognizer_names_are_normalized_and_acr_host_is_stripped() -> None:
    settings = load_settings(
        {
            "MCP_PATH_SECRET": SECRET,
            "RECOGNIZERS": " Shazam, AudD, acrcloud ",
            "AUDD_API_TOKEN": "audd-token",
            "ACR_HOST": "https://identify-eu-west-1.acrcloud.com/v1/identify",
            "ACR_ACCESS_KEY": "key",
            "ACR_ACCESS_SECRET": "secret",
        }
    )
    assert settings.recognizers == ("shazam", "audd", "acrcloud")
    assert settings.acr_host == "identify-eu-west-1.acrcloud.com"


def test_retry_offset_zero_is_allowed() -> None:
    settings = load_settings({"MCP_PATH_SECRET": SECRET, "RETRY_OFFSET_SECONDS": "0"})
    assert settings.retry_offset_seconds == 0


def test_redaction_strips_secret_and_signed_urls() -> None:
    set_redactions([SECRET, "audd-token-value"])
    try:
        text = (
            f"POST /{SECRET}/mcp https://soundcloud.com/artist/set?si=1 "
            "https://cf-media.sndcdn.com/playlist.m3u8?Policy=secret audd-token-value"
        )
        cleaned = redact_text(text)
    finally:
        set_redactions([])
    assert SECRET not in cleaned
    assert "audd-token-value" not in cleaned
    assert "Policy" not in cleaned
    assert "https://soundcloud.com/artist/set" in cleaned
    assert "https://cf-media.sndcdn.com/[redacted]" in cleaned
    assert redact_text("Uvicorn running on http://0.0.0.0:8080") == (
        "Uvicorn running on http://0.0.0.0:8080"
    )


def test_uvicorn_log_config_accepts_uppercase_levels() -> None:
    # logging.config.dictConfig rejects "info"; that used to crash startup.
    config = uvicorn_log_config("info")
    assert config["loggers"]["uvicorn"]["level"] == "INFO"
    assert config["root"]["level"] == "INFO"
    logging.config.dictConfig(config)
