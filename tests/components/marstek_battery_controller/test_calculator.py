"""Unit tests for pure calculator and laadplanning (§10)."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from custom_components.marstek_battery_controller import const
from custom_components.marstek_battery_controller.calculator import (
    CalculatorInputs,
    compute_base,
    compute_peak_window_deadline,
    compute_latest_start_charge,
    compute_setpoint,
    effective_cap_threshold_w,
    energy_needed_for_reserve_wh,
    protection_window_active,
)


def _inp(**kwargs: object) -> CalculatorInputs:
    """Factory with sane defaults for CalculatorInputs."""
    defaults = dict(
        mode=const.MODE_SELF_CONSUMPTION,
        grid_smoothed=0.0,
        battery_smoothed=0.0,
        current_soc=50.0,
        grid_sensor_ok=True,
        soc_sensor_ok=True,
        battery_sensor_ok=True,
        cap_now=0.0,
        monthly_peak_w=None,
        monthly_peak_ok=False,
        capacity_tariff_enabled=True,
        max_desired_peak_w=2500.0,
        min_soc=12.0,
        max_soc=100.0,
        max_battery_power_w=2500.0,
        reserve_target_soc=50.0,
        boost_charge_power_w=1250.0,
        manual_target_soc=50.0,
        manual_power_w=1000.0,
        reserve_protection_start=time(13, 0),
        peak_window_start=time(18, 0),
        latest_start_charge=const.LATEST_START_NO_NEED,
        peak_window_deadline=datetime(2026, 4, 27, 18, 0, tzinfo=ZoneInfo("Europe/Brussels")),
        now=datetime(2026, 4, 27, 12, 0, 0, tzinfo=ZoneInfo("Europe/Brussels")),
    )
    defaults.update(kwargs)
    return CalculatorInputs(**defaults)  # type: ignore[arg-type]


def test_compute_base() -> None:
    """§8 base = batt + grid."""
    assert compute_base(1000.0, -500.0) == pytest.approx(500.0)


def test_effective_cap_monthly() -> None:
    """§9.2 effective uses max of user and monthly peak."""
    assert effective_cap_threshold_w(2500.0, 3000.0, True) == 3000.0
    assert effective_cap_threshold_w(2500.0, None, False) == 2500.0


def test_soc_guard_floor() -> None:
    """Discharge blocked at min SoC."""
    out = compute_setpoint(
        _inp(
            mode=const.MODE_SELF_CONSUMPTION,
            grid_smoothed=2000.0,
            battery_smoothed=0.0,
            current_soc=10.0,
            min_soc=12.0,
        )
    )
    assert out.setpoint_w == 0
    assert out.reason_code == const.REASON_AT_FLOOR


def test_soc_guard_ceiling() -> None:
    """Charge blocked at max SoC."""
    out = compute_setpoint(
        _inp(
            mode=const.MODE_SELF_CONSUMPTION,
            grid_smoothed=-2000.0,
            battery_smoothed=0.0,
            current_soc=99.0,
            max_soc=99.0,
        )
    )
    assert out.setpoint_w == 0
    assert out.reason_code == const.REASON_AT_CEILING


def test_manual_bypasses_soc_guard() -> None:
    """§9 — manual bypasses SoC guard (still clamped)."""
    out = compute_setpoint(
        _inp(
            mode=const.MODE_MANUAL,
            grid_smoothed=0.0,
            battery_smoothed=0.0,
            current_soc=10.0,
            min_soc=50.0,
            manual_target_soc=90.0,
            manual_power_w=1000.0,
            max_battery_power_w=2500.0,
        )
    )
    assert out.setpoint_w is not None and out.setpoint_w < 0


def test_skip_when_grid_bad() -> None:
    """Calculator skips when grid sensor not ok."""
    out = compute_setpoint(_inp(grid_sensor_ok=False))
    assert out.setpoint_w is None


def test_evening_boost() -> None:
    """§6.3 forced charge inside boost window under cap."""
    peak = time(18, 0)
    now = datetime(2026, 4, 27, 17, 0, 0, tzinfo=ZoneInfo("Europe/Brussels"))
    deadline = compute_peak_window_deadline(peak, now)
    ls = datetime(2026, 4, 27, 16, 0, 0, tzinfo=ZoneInfo("Europe/Brussels"))
    out = compute_setpoint(
        _inp(
            mode=const.MODE_SELF_CONSUMPTION_BOOST,
            grid_smoothed=100.0,
            battery_smoothed=0.0,
            current_soc=40.0,
            max_soc=100.0,
            cap_now=100.0,
            max_desired_peak_w=2500.0,
            boost_charge_power_w=1250.0,
            latest_start_charge=ls,
            peak_window_deadline=deadline,
            now=now,
        )
    )
    assert out.setpoint_w == pytest.approx(-1250)
    assert out.reason_code == const.REASON_BOOST_ACTIVE


def test_reserve_mode_hold() -> None:
    """§6.4 clamp discharge to 0 below reserve target under cap."""
    now = datetime(2026, 4, 27, 14, 0, 0, tzinfo=ZoneInfo("Europe/Brussels"))
    assert protection_window_active(time(13, 0), time(18, 0), now) is True
    out = compute_setpoint(
        _inp(
            mode=const.MODE_SELF_CONSUMPTION_RESERVE,
            grid_smoothed=500.0,
            battery_smoothed=0.0,
            current_soc=40.0,
            reserve_target_soc=50.0,
            cap_now=100.0,
            max_desired_peak_w=2500.0,
            now=now,
        )
    )
    assert out.setpoint_w == 0
    assert out.operating_state == const.STATE_RESERVE_HELD


def test_latest_start_no_need() -> None:
    """§10 — already at target."""
    assert (
        compute_latest_start_charge(
            soc_now=55.0,
            soc_target=50.0,
            p_max_w=1250.0,
            batt_wh=5120.0,
            peak_time=time(18, 0),
            now=datetime.now(tz=ZoneInfo("Europe/Brussels")),
        )
        == const.LATEST_START_NO_NEED
    )


def test_latest_start_finite() -> None:
    """§10 — finite latest start before deadline."""
    brussels = ZoneInfo("Europe/Brussels")
    now = datetime(2026, 4, 27, 10, 0, 0, tzinfo=brussels)
    ls = compute_latest_start_charge(
        soc_now=40.0,
        soc_target=50.0,
        p_max_w=1250.0,
        batt_wh=5120.0,
        peak_time=time(18, 0),
        now=now,
    )
    assert isinstance(ls, datetime)
    assert ls.tzinfo is not None
    assert ls < datetime(2026, 4, 27, 18, 0, 0, tzinfo=brussels)


def test_energy_needed_reserve() -> None:
    """§14 energy diagnostic floored at 0."""
    assert energy_needed_for_reserve_wh(60.0, 50.0, 5000.0) == 0.0
    assert energy_needed_for_reserve_wh(40.0, 50.0, 5000.0) == pytest.approx(500.0)
