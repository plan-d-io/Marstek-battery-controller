"""Tests for sliding-window smoothing."""

from __future__ import annotations

import pytest

from custom_components.marstek_battery_controller.smoothing import SlidingWindowMean


def test_mean_single_sample_in_window() -> None:
    """Inside window a single sample yields its value."""
    sw = SlidingWindowMean(60.0)
    sw.push(1000.0, 500.0)
    assert sw.mean(1005.0, fallback=0.0) == pytest.approx(500.0)


def test_mean_two_samples_time_weighted() -> None:
    """Piecewise constant interpolation between two samples."""
    sw = SlidingWindowMean(100.0)
    sw.push(0.0, 100.0)
    sw.push(50.0, 300.0)
    # From t=50 to t=100: value 300 for 50s; from t=0-50 excluded from window_start at t=100 window is [0,100]
    # Window [0,100]: 0-50 uses 100, 50-100 uses 300 -> weighted mean
    # Actually window_start = 0 for now=100, window 100s -> includes both
    # Segment [0,50): v=100 dur 50 -> 5000
    # Segment [50,100): v=300 dur 50 -> 15000 -> total 20000/100 = 200
    assert sw.mean(100.0) == pytest.approx(200.0)


def test_old_samples_dropped() -> None:
    """Samples older than window are removed."""
    sw = SlidingWindowMean(10.0)
    sw.push(0.0, 1000.0)
    assert sw.mean(100.0, fallback=99.0) == pytest.approx(99.0)


def test_empty_fallback() -> None:
    """Empty deque uses fallback."""
    sw = SlidingWindowMean(5.0)
    assert sw.mean(10.0, fallback=42.0) == pytest.approx(42.0)


def test_clear() -> None:
    """Clear removes history."""
    sw = SlidingWindowMean(60.0)
    sw.push(1.0, 1.0)
    sw.clear()
    assert sw.mean(2.0, fallback=0.0) == 0.0
