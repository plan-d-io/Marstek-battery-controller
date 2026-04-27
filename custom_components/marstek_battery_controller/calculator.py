"""Pure setpoint calculation for the Marstek Battery Controller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time
from typing import Literal

from . import const


@dataclass(frozen=True)
class CalculatorInputs:
    """Numeric and state inputs for one calculation pass."""

    mode: str
    grid_smoothed: float
    battery_smoothed: float
    current_soc: float
    grid_sensor_ok: bool
    soc_sensor_ok: bool
    battery_sensor_ok: bool  # If False, battery_smoothed should be 0 per caller.
    cap_now: float
    monthly_peak_w: float | None
    monthly_peak_ok: bool
    capacity_tariff_enabled: bool
    max_desired_peak_w: float
    min_soc: float
    max_soc: float
    max_battery_power_w: float
    evening_min_soc: float
    evening_max_charge_power_w: float
    manual_target_soc: float
    manual_power_w: float
    passive_floor_start: time
    evening_peak_start: time
    latest_start_charge: datetime | Literal["no_need"]
    evening_peak_deadline: datetime
    now: datetime


@dataclass(frozen=True)
class CalculatorOutput:
    """Result of one setpoint calculation."""

    setpoint_w: int | None
    """Signed watts to request; None means skip update (hold previous target)."""

    operating_state: str
    reason_code: str


def compute_base(grid_smoothed: float, battery_smoothed: float) -> float:
    """§8 — base setpoint before mode override and guards."""
    return battery_smoothed + grid_smoothed


def effective_cap_threshold_w(
    max_desired_peak_w: float,
    monthly_peak_w: float | None,
    monthly_peak_ok: bool,
) -> float:
    """§9.2 — effective capacity-tariff ceiling in watts."""
    effective = max_desired_peak_w
    if monthly_peak_ok and monthly_peak_w is not None:
        effective = max(effective, monthly_peak_w)
    return effective


def compute_latest_start_charge(
    *,
    soc_now: float,
    soc_target: float,
    p_max_w: float,
    batt_wh: float,
    peak_time: time,
    now: datetime,
) -> datetime | Literal["no_need"]:
    """§10 — latest time to start grid charging to reach soc_target by peak_time."""
    if soc_target <= soc_now:
        return const.LATEST_START_NO_NEED

    dsoc = soc_target - soc_now
    energy_needed_wh = (dsoc / 100.0) * batt_wh
    if p_max_w <= 0:
        # Avoid division by zero; invalid power — caller should validate inputs.
        seconds = float("inf")
    else:
        seconds = (energy_needed_wh / p_max_w) * 3600.0

    if seconds == float("inf"):
        # Cannot compute a finite start; treat as no scheduled boost.
        return const.LATEST_START_NO_NEED

    tz = now.tzinfo
    today_peak = datetime.combine(now.date(), peak_time, tzinfo=tz)
    if today_peak > now:
        deadline = today_peak
    else:
        deadline = today_peak + timedelta(days=1)

    latest_start = deadline - timedelta(seconds=seconds)
    return latest_start


def compute_evening_peak_deadline(evening_peak_start: time, now: datetime) -> datetime:
    """Today's or tomorrow's occurrence of evening_peak_start for window math."""
    tz = now.tzinfo
    today_peak = datetime.combine(now.date(), evening_peak_start, tzinfo=tz)
    if today_peak > now:
        return today_peak
    return today_peak + timedelta(days=1)


def boost_window_active(
    latest_start: datetime | Literal["no_need"],
    deadline: datetime,
    now: datetime,
) -> bool:
    """§6.3 — boost_active predicate."""
    if latest_start == const.LATEST_START_NO_NEED:
        return False
    return bool(now >= latest_start and now < deadline)


def protection_window_active(
    passive_floor_start: time,
    evening_peak_start: time,
    now: datetime,
) -> bool:
    """§6.4 — protection window [passive_floor_start, evening_peak_start)."""
    tz = now.tzinfo
    today = now.date()
    start_dt = datetime.combine(today, passive_floor_start, tzinfo=tz)
    end_dt = datetime.combine(today, evening_peak_start, tzinfo=tz)
    # Normalize if protection start is after peak on calendar day — spec §16: empty window.
    if start_dt >= end_dt:
        return False
    return bool(start_dt <= now < end_dt)


def minutes_until_evening_peak(evening_peak_start: time, now: datetime) -> float:
    """Minutes until next evening_peak_start."""
    tz = now.tzinfo
    today_peak = datetime.combine(now.date(), evening_peak_start, tzinfo=tz)
    target = today_peak if today_peak > now else today_peak + timedelta(days=1)
    return max(0.0, (target - now).total_seconds() / 60.0)


def energy_needed_for_evening_wh(
    current_soc: float,
    evening_min_soc: float,
    battery_capacity_wh: float,
) -> float:
    """Diagnostic §14 — energy needed to reach evening floor."""
    need = (evening_min_soc - current_soc) / 100.0 * battery_capacity_wh
    return max(0.0, need)


def _clamp_float(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _soc_guard(
    setpoint: float,
    current_soc: float,
    min_soc: float,
    max_soc: float,
) -> tuple[float, str]:
    """§9.1 — asymmetric SoC guard; returns (setpoint, reason_if_special)."""
    reason = const.REASON_NORMAL
    if current_soc <= min_soc and setpoint > 0:
        return 0.0, const.REASON_AT_FLOOR
    if current_soc >= max_soc and setpoint < 0:
        return 0.0, const.REASON_AT_CEILING
    return setpoint, reason


def compute_setpoint(inp: CalculatorInputs) -> CalculatorOutput:
    """Full pipeline: base → mode override → guards → clamp."""
    if inp.mode == const.MODE_RELEASED:
        return CalculatorOutput(
            setpoint_w=0,
            operating_state=const.STATE_RELEASED,
            reason_code=const.REASON_RELEASED,
        )

    if not inp.grid_sensor_ok or not inp.soc_sensor_ok:
        return CalculatorOutput(
            setpoint_w=None,
            operating_state=_operating_state_for_mode(inp.mode, inp, provisional=True),
            reason_code=const.REASON_NORMAL,
        )

    grid = inp.grid_smoothed
    batt = inp.battery_smoothed if inp.battery_sensor_ok else 0.0
    base = compute_base(grid, batt)
    effective = effective_cap_threshold_w(
        inp.max_desired_peak_w,
        inp.monthly_peak_w,
        inp.monthly_peak_ok,
    )

    setpoint_f, op_state, reason_mode = _mode_override(
        base,
        inp,
        effective,
    )

    reason = reason_mode
    # §9 — Manual bypasses SoC and capacity-tariff guards (not the power clamp).
    if inp.mode != const.MODE_MANUAL:
        setpoint_f, r = _soc_guard(
            setpoint_f,
            inp.current_soc,
            inp.min_soc,
            inp.max_soc,
        )
        if r != const.REASON_NORMAL:
            reason = r

    clamped_f = _clamp_float(setpoint_f, -inp.max_battery_power_w, inp.max_battery_power_w)
    clamped = int(round(clamped_f))

    return CalculatorOutput(
        setpoint_w=clamped,
        operating_state=op_state,
        reason_code=reason,
    )


def _operating_state_for_mode(mode: str, inp: CalculatorInputs, provisional: bool) -> str:
    if provisional:
        if mode == const.MODE_SELF_CONSUMPTION:
            return const.STATE_SELF_CONSUMPTION
        if mode == const.MODE_SELF_CONSUMPTION_EVENING_PEAK:
            return const.STATE_SELF_CONSUMPTION
        if mode == const.MODE_SELF_CONSUMPTION_PASSIVE_EVENING_PEAK:
            return const.STATE_SELF_CONSUMPTION
        if mode == const.MODE_MANUAL:
            return const.STATE_MANUAL_CHARGING
    return const.STATE_SELF_CONSUMPTION


def _mode_override(
    base: float,
    inp: CalculatorInputs,
    effective_cap: float,
) -> tuple[float, str, str]:
    """Apply §6 mode-specific rules; returns (setpoint_float, operating_state, reason)."""
    mode = inp.mode
    reason = const.REASON_NORMAL

    if mode == const.MODE_SELF_CONSUMPTION:
        return base, const.STATE_SELF_CONSUMPTION, reason

    if mode == const.MODE_SELF_CONSUMPTION_EVENING_PEAK:
        deadline = inp.evening_peak_deadline
        boost_on = boost_window_active(inp.latest_start_charge, deadline, inp.now)
        inside = boost_on and inp.current_soc < inp.max_soc
        under_cap = (not inp.capacity_tariff_enabled) or (inp.cap_now < effective_cap)
        if inside and under_cap:
            sp = -float(inp.evening_max_charge_power_w)
            return (
                sp,
                const.STATE_PRE_CHARGING,
                const.REASON_BOOST_ACTIVE,
            )
        if inside and inp.capacity_tariff_enabled and inp.cap_now >= effective_cap:
            return base, const.STATE_SELF_CONSUMPTION, const.REASON_CAP_TARIFF
        return base, const.STATE_SELF_CONSUMPTION, reason

    if mode == const.MODE_SELF_CONSUMPTION_PASSIVE_EVENING_PEAK:
        if not protection_window_active(
            inp.passive_floor_start,
            inp.evening_peak_start,
            inp.now,
        ):
            return base, const.STATE_SELF_CONSUMPTION, reason
        # Inside protection window
        if (
            base > 0
            and inp.current_soc <= inp.evening_min_soc
            and ((not inp.capacity_tariff_enabled) or (inp.cap_now < effective_cap))
        ):
            return 0.0, const.STATE_FLOOR_PROTECTION, const.REASON_FLOOR_HELD
        if (
            base > 0
            and inp.capacity_tariff_enabled
            and inp.cap_now >= effective_cap
        ):
            # Floor clamp does not apply — allow discharge
            return base, const.STATE_SELF_CONSUMPTION, const.REASON_CAP_TARIFF
        return base, const.STATE_SELF_CONSUMPTION, reason

    if mode == const.MODE_MANUAL:
        target = inp.manual_target_soc
        cur = inp.current_soc
        if round(cur) == round(target):
            return 0.0, const.STATE_MANUAL_CHARGING, const.REASON_MANUAL_ACTIVE
        mag = float(inp.manual_power_w)
        if target > cur:
            sp = -mag
            return sp, const.STATE_MANUAL_CHARGING, const.REASON_MANUAL_ACTIVE
        sp = mag
        return sp, const.STATE_MANUAL_DISCHARGING, const.REASON_MANUAL_ACTIVE

    return base, const.STATE_SELF_CONSUMPTION, reason

