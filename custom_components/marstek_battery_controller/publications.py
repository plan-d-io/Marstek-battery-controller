"""Publication identity and freshness for battery-control inputs.

Grid, measured battery power, and battery state of charge each carry their own
monotonic publication time. A repeated Home Assistant state, an older sample,
or a sample that is already stale is not a new feedback publication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# Freshness windows (seconds). Grid matches the integration's fast-poll limit.
# Battery telemetry is slower than the grid meter; SoC moves slower still.
DEFAULT_GRID_FRESH_S: Final = 5.0
DEFAULT_BATTERY_FRESH_S: Final = 10.0
DEFAULT_SOC_FRESH_S: Final = 30.0
# A slow but regular sensor stays fresh for several of its own periods.
CADENCE_FRESH_MULT: Final = 3.0
MIN_OBSERVED_CADENCE_S: Final = 0.2
MAX_OBSERVED_CADENCE_S: Final = 60.0

CHANNELS: Final[tuple[str, ...]] = ("grid", "battery", "soc")


@dataclass
class Sample:
    """Latest observation of one input channel."""

    value: float | None = None
    source_mono: float | None = None
    receive_mono: float | None = None
    publication_id: str | None = None
    available: bool = False
    observed_cadence_s: float | None = None
    # True only when the stored sample is a fresh publication not yet consumed.
    new_feedback: bool = False

    def age_s(self, now_mono: float) -> float | None:
        """Age of the publication clock, or None when no sample has been stored."""
        if self.source_mono is None:
            return None
        return max(0.0, now_mono - self.source_mono)

    def effective_fresh_limit_s(self, base_limit_s: float) -> float:
        """Cadence-aware freshness limit."""
        if self.observed_cadence_s is None:
            return base_limit_s
        return max(base_limit_s, self.observed_cadence_s * CADENCE_FRESH_MULT)

    def is_fresh(self, now_mono: float, limit_s: float) -> bool:
        """Whether this sample may be used as live feedback at ``now_mono``."""
        if not self.available or self.value is None or self.source_mono is None:
            return False
        return (now_mono - self.source_mono) <= self.effective_fresh_limit_s(limit_s)


@dataclass(frozen=True)
class PublishResult:
    """Outcome of offering one observation to the tracker."""

    recorded: bool
    new_feedback: bool


@dataclass
class PublicationTracker:
    """Monotonic publication tracking for grid, battery power, and SoC."""

    grid: Sample = field(default_factory=Sample)
    battery: Sample = field(default_factory=Sample)
    soc: Sample = field(default_factory=Sample)
    grid_fresh_s: float = DEFAULT_GRID_FRESH_S
    battery_fresh_s: float = DEFAULT_BATTERY_FRESH_S
    soc_fresh_s: float = DEFAULT_SOC_FRESH_S
    last_integrated_grid_source_mono: float | None = None
    last_integrated_grid_publication_id: str | None = None

    def _sample(self, channel: str) -> Sample:
        if channel not in CHANNELS:
            raise ValueError(f"unknown measurement channel: {channel}")
        sample = getattr(self, channel)
        assert isinstance(sample, Sample)
        return sample

    def _limit(self, channel: str) -> float:
        return {
            "grid": self.grid_fresh_s,
            "battery": self.battery_fresh_s,
            "soc": self.soc_fresh_s,
        }[channel]

    def publish(
        self,
        channel: str,
        value: float,
        *,
        source_mono: float,
        receive_mono: float,
        publication_id: str | None = None,
        wall_age_s: float | None = None,
    ) -> PublishResult:
        """Store an observation. Stale or repeated samples are not new feedback.

        ``source_mono`` is the monotonic publication time. When ``wall_age_s`` is
        set (Home Assistant ``last_reported`` age), the effective publication
        time is ``receive_mono - wall_age_s`` so an already-old value enters stale.
        """
        sample = self._sample(channel)
        if publication_id is not None and sample.publication_id == publication_id:
            sample.receive_mono = receive_mono
            return PublishResult(recorded=False, new_feedback=False)

        effective_source = float(source_mono)
        if wall_age_s is not None and wall_age_s >= 0:
            effective_source = float(receive_mono) - float(wall_age_s)

        if sample.source_mono is not None and effective_source <= sample.source_mono + 1e-9:
            sample.receive_mono = receive_mono
            return PublishResult(recorded=False, new_feedback=False)

        if sample.source_mono is not None:
            dt = effective_source - sample.source_mono
            if MIN_OBSERVED_CADENCE_S <= dt <= MAX_OBSERVED_CADENCE_S:
                if sample.observed_cadence_s is None:
                    sample.observed_cadence_s = dt
                else:
                    sample.observed_cadence_s = 0.7 * sample.observed_cadence_s + 0.3 * dt

        sample.value = float(value)
        sample.source_mono = effective_source
        sample.receive_mono = receive_mono
        sample.publication_id = publication_id
        sample.available = True
        fresh = sample.is_fresh(receive_mono, self._limit(channel))
        sample.new_feedback = fresh
        return PublishResult(recorded=True, new_feedback=fresh)

    def mark_unavailable(self, channel: str) -> None:
        """Drop the channel so it cannot be treated as feedback."""
        sample = self._sample(channel)
        sample.available = False
        sample.value = None
        sample.new_feedback = False

    def grid_is_fresh(self, now_mono: float) -> bool:
        return self.grid.is_fresh(now_mono, self.grid_fresh_s)

    def battery_is_fresh(self, now_mono: float) -> bool:
        return self.battery.is_fresh(now_mono, self.battery_fresh_s)

    def soc_is_fresh(self, now_mono: float) -> bool:
        return self.soc.is_fresh(now_mono, self.soc_fresh_s)

    def should_integrate_grid(self, now_mono: float) -> bool:
        """True only for a fresh grid publication not yet consumed."""
        if not self.grid.new_feedback or not self.grid_is_fresh(now_mono):
            return False
        src = self.grid.source_mono
        if src is None:
            return False
        if (
            self.grid.publication_id is not None
            and self.grid.publication_id == self.last_integrated_grid_publication_id
        ):
            return False
        if self.last_integrated_grid_source_mono is None:
            return True
        return src > self.last_integrated_grid_source_mono + 1e-9

    def mark_grid_integrated(self) -> None:
        """Remember that the current grid publication has advanced the controller."""
        self.last_integrated_grid_source_mono = self.grid.source_mono
        self.last_integrated_grid_publication_id = self.grid.publication_id
        self.grid.new_feedback = False
