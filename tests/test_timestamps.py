"""Timestamp parsing and formatting."""

import pytest

from track_id.errors import UserInputError
from track_id.timestamps import format_timestamp, parse_timestamp


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("42:10", 2530),
        ("1:02:30", 3750),
        ("2530", 2530),
        ("2530.5", 2530.5),
        ("0", 0),
        ("0:00", 0),
        ("00:00:00", 0),
        (" 42:10 ", 2530),
        ("1:2:3", 3723),
        ("1:02:30.5", 3750.5),
        ("90:00", 5400),
        ("59.9", 59.9),
    ],
)
def test_parse_accepts_documented_forms(raw: str, seconds: float) -> None:
    assert parse_timestamp(raw) == pytest.approx(seconds)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "-1",
        "+10",
        "abc",
        "1:60",
        "1:02:60",
        "1:",
        ":10",
        "1:2:3:4",
        "1e3",
        "42: 10",
        None,
        12,
    ],
)
def test_parse_rejects_bad_values(raw: object) -> None:
    with pytest.raises(UserInputError):
        parse_timestamp(raw)


def test_parse_rejects_absurdly_long_values() -> None:
    with pytest.raises(UserInputError):
        parse_timestamp("200000")


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0:00"),
        (9, "0:09"),
        (65, "1:05"),
        (2530, "42:10"),
        (3750, "1:02:30"),
        (3600, "1:00:00"),
        (2530.5, "42:10.5"),
        (59.96, "1:00"),
        (3661, "1:01:01"),
    ],
)
def test_format_clock(seconds: float, text: str) -> None:
    assert format_timestamp(seconds) == text


def test_round_trip_clock_values() -> None:
    for raw in ("0:00", "42:10", "1:02:30", "12:00:00"):
        assert format_timestamp(parse_timestamp(raw)) == raw
