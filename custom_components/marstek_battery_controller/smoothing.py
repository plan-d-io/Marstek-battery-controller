"""Sliding-window smoothing for grid and battery power sensors."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque


@dataclass(frozen=True)
class TimestampedSample:
    """A single sensor reading at a point in time."""

    timestamp: float
    value: float


class SlidingWindowMean:
    """Deque of (timestamp, value); time-weighted mean over the active window."""

    def __init__(self, window_seconds: float) -> None:
        """Initialize an empty sliding window.

        Args:
            window_seconds: Window length in seconds; samples older than this are dropped.
        """
        self._window_seconds = window_seconds
        self._samples: Deque[TimestampedSample] = deque()

    @property
    def window_seconds(self) -> float:
        """Configured window length in seconds."""
        return self._window_seconds

    def set_window_seconds(self, window_seconds: float) -> None:
        """Resize the window (does not clear samples)."""
        self._window_seconds = window_seconds

    def push(self, timestamp: float, value: float) -> None:
        """Append a sample (typically on each state_changed)."""
        self._samples.append(TimestampedSample(timestamp=timestamp, value=value))

    def clear(self) -> None:
        """Remove all samples."""
        self._samples.clear()

    def mean(
        self,
        now: float,
        *,
        fallback: float | None = None,
    ) -> float:
        """Return the time-weighted mean over [now - window, now].

        Piecewise-constant interpolation between samples: value v_i applies from t_i
        until the next sample. Segments are weighted by duration overlapping the window.

        If no samples remain in the window and fallback is not None, returns fallback.
        If no samples and fallback is None, returns 0.0.
        """
        window_start = now - self._window_seconds
        while self._samples and self._samples[0].timestamp < window_start:
            self._samples.popleft()

        if not self._samples:
            return 0.0 if fallback is None else float(fallback)

        # Single sample: constant over window segment [max(t0, window_start), now]
        if len(self._samples) == 1:
            s = self._samples[0]
            seg_start = max(s.timestamp, window_start)
            if now <= seg_start:
                return float(s.value)
            duration = now - seg_start
            if duration <= 0:
                return float(s.value)
            return float(s.value)

        # Multiple samples: integrate piecewise constant value over [window_start, now]
        pts = list(self._samples)
        total_weighted = 0.0
        total_duration = 0.0

        for i in range(len(pts)):
            t_i = pts[i].timestamp
            v_i = pts[i].value
            t_next = pts[i + 1].timestamp if i + 1 < len(pts) else now
            seg_start = max(t_i, window_start)
            seg_end = min(t_next, now)
            if seg_end > seg_start:
                dur = seg_end - seg_start
                total_weighted += v_i * dur
                total_duration += dur

        if total_duration <= 0:
            return float(pts[-1].value)

        return total_weighted / total_duration
