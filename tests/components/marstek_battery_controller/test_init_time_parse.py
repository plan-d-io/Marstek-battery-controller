"""Tests for config-entry time option parsing."""

from __future__ import annotations

from datetime import datetime, time

from custom_components.marstek_battery_controller.__init__ import _parse_wall_clock_time


def test_parse_hh_mm() -> None:
    assert _parse_wall_clock_time("18:00") == time(18, 0)


def test_parse_hh_mm_ss() -> None:
    assert _parse_wall_clock_time("18:00:00") == time(18, 0, 0)
    assert _parse_wall_clock_time("13:00:00") == time(13, 0, 0)


def test_parse_fractional() -> None:
    assert _parse_wall_clock_time("18:00:00.123456") == time(
        18, 0, 0, 123456
    )


def test_parse_time_object() -> None:
    assert _parse_wall_clock_time(time(9, 30)) == time(9, 30)


def test_parse_datetime() -> None:
    dt = datetime(2026, 5, 2, 14, 45, 30)
    assert _parse_wall_clock_time(dt) == time(14, 45, 30)


def test_parse_invalid() -> None:
    assert _parse_wall_clock_time("") is None
    assert _parse_wall_clock_time("nope") is None
    assert _parse_wall_clock_time(123) is None
