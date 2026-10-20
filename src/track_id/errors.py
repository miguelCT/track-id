"""Exceptions that map to tool responses or startup failures."""


class ConfigError(Exception):
    """Environment is missing or invalid. The process should exit."""


class UserInputError(Exception):
    """The caller's URL or timestamp is unusable. Safe to show back to them."""


class AudioError(Exception):
    """Resolve or ffmpeg failed. The message is already safe to show."""


class StreamExpiredError(AudioError):
    """The signed SoundCloud stream URL was rejected. Re-resolve and retry once."""


class RecognizerError(Exception):
    """One recognition backend failed. The chain should try the next one."""
