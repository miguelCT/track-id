"""Parse and format the timestamps Claude sends and reads back."""

from __future__ import annotations

import re

from track_id.errors import UserInputError

_HELP = 'Use "42:10", "1:02:30", "2530", or "2530.5".'
_SECONDS = re.compile(r"^(\d+(?:\.\d+)?)$")
_MAX_SECONDS = 48 * 60 * 60


def parse_timestamp(value: object) -> float:
    """Parse `"42:10"`, `"1:02:30"`, `"2530"`, or `"2530.5"` into seconds.

    Clock fields other than the last one must be whole numbers. Minutes and
    seconds in a clock timestamp must be < 60. A bare number is already seconds
    and may be larger than 60. Negative values are rejected.
    """

    if not isinstance(value, str):
        raise UserInputError(f"Timestamp must be a string. {_HELP}")
    text = value.strip()
    if not text:
        raise UserInputError(f"Timestamp is empty. {_HELP}")
    if text[0] in "+-" or any(ch.isspace() for ch in text):
        raise UserInputError(f"Timestamp cannot be negative or contain spaces. {_HELP}")

    seconds_match = _SECONDS.fullmatch(text)
    if seconds_match:
        return _check_magnitude(float(seconds_match.group(1)))

    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise UserInputError(f"Could not parse timestamp {value!r}. {_HELP}")
    try:
        numbers = [
            _parse_part(part, fractional=index == len(parts) - 1)
            for index, part in enumerate(parts)
        ]
    except ValueError as exc:
        raise UserInputError(f"Could not parse timestamp {value!r}. {_HELP}") from exc

    if len(parts) == 2:
        minutes, seconds = numbers
        if seconds >= 60:
            raise UserInputError("Seconds must be less than 60 in an m:ss timestamp.")
        total = minutes * 60 + seconds
    else:
        hours, minutes, seconds = numbers
        if minutes >= 60 or seconds >= 60:
            raise UserInputError(
                "Minutes and seconds must be less than 60 in an h:mm:ss timestamp."
            )
        total = hours * 3600 + minutes * 60 + seconds
    return _check_magnitude(total)


def format_timestamp(seconds: float) -> str:
    """Format seconds as `m:ss` or `h:mm:ss`. Tenths are kept when they are not zero."""

    if seconds < 0 or seconds != seconds or seconds == float("inf"):
        raise ValueError(f"Cannot format timestamp {seconds!r}.")
    rounded = round(float(seconds), 1)
    whole = int(rounded)
    tenth = int(round((rounded - whole) * 10))
    if tenth == 10:
        whole += 1
        tenth = 0
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    body = f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
    if tenth:
        body = f"{body}.{tenth}"
    return body


def _parse_part(part: str, *, fractional: bool) -> float:
    if not part or (not fractional and "." in part):
        raise ValueError(part)
    number = float(part) if fractional else int(part, 10)
    if number < 0:
        raise ValueError(part)
    return float(number)


def _check_magnitude(seconds: float) -> float:
    if seconds > _MAX_SECONDS:
        raise UserInputError(
            "That timestamp is longer than 48 hours, which this server will not scan."
        )
    return seconds
