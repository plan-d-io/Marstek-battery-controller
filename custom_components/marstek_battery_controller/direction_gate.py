"""Charge/discharge flip limiter — gate between calculator and setpoint scheduler."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from . import const

DirectionBucket = Literal["charge", "discharge", "neutral"]


@dataclass
class DirectionGateState:
    """Sliding-window timestamps of allowed charge ↔ discharge flips."""

    flip_times: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class DirectionGateResult:
    """Whether the calculator setpoint may update ``target_setpoint``."""

    apply: bool
    reason_code: str | None = None


def _bucket(setpoint_w: int) -> DirectionBucket:
    if setpoint_w > 0:
        return "discharge"
    if setpoint_w < 0:
        return "charge"
    return "neutral"


def _is_charge_discharge_flip(a: DirectionBucket, b: DirectionBucket) -> bool:
    return (a == "charge" and b == "discharge") or (a == "discharge" and b == "charge")


def _prune_flip_times(state: DirectionGateState, now_mono: float, window_s: float) -> None:
    cutoff = now_mono - window_s
    state.flip_times = [t for t in state.flip_times if t > cutoff]


def evaluate_direction_gate(
    mode: str,
    raw_setpoint_w: int,
    current_target_w: int,
    state: DirectionGateState,
    now_mono: float,
    *,
    window_s: float = const.DIRECTION_COOLDOWN_WINDOW_S,
    max_flips: int = const.DIRECTION_COOLDOWN_MAX_FLIPS,
) -> DirectionGateResult:
    """Return whether ``raw_setpoint_w`` may replace ``current_target_w``.

    Only active in self-consumption modes. Released and manual bypass the gate.
    While the gate is closed (≥ ``max_flips`` in the window), no target update is
  applied so the tick writer does not send Modbus. Neutral (0) is not a flip.
    """
    if mode not in const.DIRECTION_GATE_MODES:
        return DirectionGateResult(apply=True)

    _prune_flip_times(state, now_mono, window_s)

    if len(state.flip_times) >= max_flips:
        return DirectionGateResult(
            apply=False,
            reason_code=const.REASON_DIRECTION_COOLDOWN,
        )

    current_b = _bucket(current_target_w)
    raw_b = _bucket(raw_setpoint_w)
    if _is_charge_discharge_flip(current_b, raw_b):
        state.flip_times.append(now_mono)

    return DirectionGateResult(apply=True)
