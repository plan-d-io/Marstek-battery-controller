"""Pure EV-exclusion feedback. No Home Assistant runtime."""

from __future__ import annotations

import math

import pytest

from custom_components.marstek_battery_controller.ev_feedback import (
    EV_IDLE_FLOOR_W,
    EvExclusionFeedback,
    EvPowerVerdict,
    NormalizedEvPower,
    ev_exclusion_feedback,
    known_ev_feedback,
    normalize_battery_measurement,
    normalize_ev_power,
    unusable_ev_feedback,
)
from custom_components.marstek_battery_controller.non_delivery import IDLE_MEASURED_W


def test_idle_floor_matches_measured_battery_idle() -> None:
    assert EV_IDLE_FLOOR_W == IDLE_MEASURED_W == 25.0


def test_ev_off_house_importing() -> None:
    result = known_ev_feedback(1500, 0, 0)
    assert result.feedback_w == 1500
    assert result.target_battery_w == 1500
    assert result.allow_discharge is True
    assert result.allow_charge is True
    assert result.request_released is False


def test_ev_only_from_grid() -> None:
    result = known_ev_feedback(7000, 0, 7000)
    assert result.target_battery_w == 0
    assert result.feedback_w == 0
    assert result.allow_discharge is True
    assert result.allow_charge is False


def test_house_and_ev_importing() -> None:
    result = known_ev_feedback(8200, 0, 7000)
    assert result.target_battery_w == 1200
    assert result.feedback_w == 1200
    assert result.allow_discharge is True
    assert result.allow_charge is False


def test_pv_covers_house_and_part_of_ev_is_not_grid_minus_ev() -> None:
    grid, ev = 4500, 7000
    result = known_ev_feedback(grid, 0, ev)
    assert result.target_battery_w == 0
    assert result.feedback_w == 0
    assert result.feedback_w != grid - ev
    assert result.allow_discharge is True
    assert result.allow_charge is False


def test_house_load_with_partial_pv_and_ev() -> None:
    """House 2000 W, PV 500 W, EV 7000 W, battery idle: grid is +8500 W."""
    result = known_ev_feedback(8500, 0, 7000)
    assert result.site_balance_w == 8500
    assert result.target_battery_w == 1500
    assert result.feedback_w == 1500


def test_export_after_house_and_ev_is_not_grid_minus_ev() -> None:
    """PV covers house and EV with 2500 W of physical export."""
    grid, ev = -2500, 7000
    result = known_ev_feedback(grid, 0, ev)
    assert result.target_battery_w == -2500
    assert result.feedback_w == -2500
    assert result.feedback_w != -9500
    assert result.feedback_w != grid - ev
    assert result.allow_charge is True
    assert result.allow_discharge is True


def test_battery_already_discharging_too_much() -> None:
    result = known_ev_feedback(5400, 2000, 7000)
    assert result.site_balance_w == 7400
    assert result.target_battery_w == 400
    assert result.feedback_w == -1600
    assert result.allow_discharge is True
    assert result.allow_charge is False


def test_battery_discharge_entirely_feeds_ev() -> None:
    """Trustworthy EV data slews 2000 W of discharge to zero through PD."""
    result = known_ev_feedback(5000, 2000, 7000)
    assert result.target_battery_w == 0
    assert result.feedback_w == -2000
    assert result.allow_discharge is True
    assert result.allow_charge is False


def test_battery_charging_while_real_surplus_remains() -> None:
    result = known_ev_feedback(1000, -2600, 2000)
    assert result.site_balance_w == -1600
    assert result.target_battery_w == -1600
    assert result.feedback_w == 1000
    assert result.allow_charge is True
    assert result.allow_discharge is True


def test_surplus_disappears_while_battery_is_charging() -> None:
    """Import returns while the battery is still charging.

    G=+2500, B=-1500, E=2000. N=+1000, so charging is closed and the target
    is 0 W. Discharge stays open so PD can slew toward that target.
    """
    result = known_ev_feedback(2500, -1500, 2000)
    assert result.site_balance_w == 1000
    assert result.site_balance_w >= 0
    assert result.target_battery_w == 0
    assert result.feedback_w == 1500
    assert result.allow_charge is False
    assert result.allow_discharge is True
    assert result.request_released is False


def test_target_cannot_be_negative_when_site_balance_is_nonnegative() -> None:
    samples = (
        (0, 0, 0),
        (1500, 0, 0),
        (7000, 0, 7000),
        (4500, 0, 7000),
        (5400, 2000, 7000),
        (2500, -1500, 2000),
        (100, -100, 50),
    )
    for grid, battery, ev in samples:
        result = known_ev_feedback(grid, battery, ev)
        assert result.site_balance_w is not None
        assert result.site_balance_w >= 0
        assert result.target_battery_w is not None
        assert result.target_battery_w >= 0
        assert result.allow_discharge is True
        assert result.allow_charge is (ev == 0)


def test_target_equals_site_balance_when_exporting() -> None:
    samples = (
        (-2500, 0, 7000),
        (1000, -2600, 2000),
        (-100, 0, 0),
        (-50, -25, 10),
    )
    for grid, battery, ev in samples:
        result = known_ev_feedback(grid, battery, ev)
        assert result.site_balance_w is not None
        assert result.site_balance_w < 0
        assert result.target_battery_w == result.site_balance_w
        assert result.allow_charge is True
        assert result.allow_discharge is True


def test_unusable_ev_with_grid_import_closes_both_directions() -> None:
    result = unusable_ev_feedback(1500, grid_fresh=True)
    assert result.allow_discharge is False
    assert result.allow_charge is False
    assert result.feedback_w is None
    assert result.request_released is False


def test_unusable_ev_with_fresh_export_keeps_charging() -> None:
    result = unusable_ev_feedback(-1800, grid_fresh=True)
    assert result.allow_discharge is False
    assert result.allow_charge is True
    assert result.feedback_w == -1800
    assert result.request_released is False


def test_unusable_ev_with_stale_negative_grid_does_not_charge() -> None:
    result = unusable_ev_feedback(-1800, grid_fresh=False)
    assert result.allow_discharge is False
    assert result.allow_charge is False
    assert result.feedback_w is None
    assert result.request_released is False


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (7000, "W", NormalizedEvPower(EvPowerVerdict.POSITIVE, 7000.0)),
        (7000, "w", NormalizedEvPower(EvPowerVerdict.POSITIVE, 7000.0)),
        (7, "kW", NormalizedEvPower(EvPowerVerdict.POSITIVE, 7000.0)),
        (7, "KW", NormalizedEvPower(EvPowerVerdict.POSITIVE, 7000.0)),
        ("1.5", "kw", NormalizedEvPower(EvPowerVerdict.POSITIVE, 1500.0)),
        (0, "W", NormalizedEvPower(EvPowerVerdict.ZERO, 0.0)),
        (0.0, "kW", NormalizedEvPower(EvPowerVerdict.ZERO, 0.0)),
        ("0", "W", NormalizedEvPower(EvPowerVerdict.ZERO, 0.0)),
    ],
)
def test_w_and_kw_conversion_and_numeric_zero(
    value: object,
    unit: str,
    expected: NormalizedEvPower,
) -> None:
    assert normalize_ev_power(value, unit) == expected


@pytest.mark.parametrize(
    ("watts", "expected"),
    [
        (25, NormalizedEvPower(EvPowerVerdict.POSITIVE, 25.0)),
        (24, NormalizedEvPower(EvPowerVerdict.ZERO, 0.0)),
        (24.999, NormalizedEvPower(EvPowerVerdict.ZERO, 0.0)),
        (0, NormalizedEvPower(EvPowerVerdict.ZERO, 0.0)),
        (-24, NormalizedEvPower(EvPowerVerdict.ZERO, 0.0)),
        (-24.999, NormalizedEvPower(EvPowerVerdict.ZERO, 0.0)),
        (-25, NormalizedEvPower(EvPowerVerdict.UNUSABLE, None)),
        (-25.001, NormalizedEvPower(EvPowerVerdict.UNUSABLE, None)),
        (-7000, NormalizedEvPower(EvPowerVerdict.UNUSABLE, None)),
    ],
)
def test_idle_floor_and_negative_boundary(watts: float, expected: NormalizedEvPower) -> None:
    assert normalize_ev_power(watts, "W") == expected
    assert normalize_ev_power(watts / 1000.0, "kW") == expected


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        (1500, None),
        (1500, ""),
        (1500, " "),
        (1500, "Wh"),
        (1.5, "kWh"),
        (10, "A"),
        (10, "V"),
        (10, "watt"),
        ("fast", "W"),
        ("", "W"),
        (None, "W"),
        (True, "W"),
        (math.nan, "W"),
        (math.inf, "W"),
        (-math.inf, "kW"),
        ("nan", "W"),
        ("inf", "W"),
    ],
)
def test_missing_bad_and_non_finite_readings_are_unusable(value: object, unit: str | None) -> None:
    assert normalize_ev_power(value, unit) == NormalizedEvPower(EvPowerVerdict.UNUSABLE, None)


def test_negative_ev_is_not_absolute_valued() -> None:
    assert normalize_ev_power(-25, "W").watts is None
    assert normalize_ev_power(-7, "kW").watts is None
    with pytest.raises(ValueError):
        known_ev_feedback(1000, 0, -25)


def test_decision_uses_normalized_zero_as_known_feedback() -> None:
    ev = normalize_ev_power(24, "W")
    result = ev_exclusion_feedback(1500, 0, ev, grid_fresh=True)
    assert ev.verdict is EvPowerVerdict.ZERO
    assert result == known_ev_feedback(1500, 0, 0)


def test_decision_routes_unusable_power_without_requesting_released() -> None:
    ev = normalize_ev_power(-25, "W")
    exporting = ev_exclusion_feedback(-900, 0, ev, grid_fresh=True)
    importing = ev_exclusion_feedback(900, 0, ev, grid_fresh=True)
    assert exporting == unusable_ev_feedback(-900, grid_fresh=True)
    assert importing == unusable_ev_feedback(900, grid_fresh=True)
    assert exporting.request_released is False
    assert importing.request_released is False


def test_usable_ev_zero_permits_both_directions() -> None:
    result = known_ev_feedback(-400, 100, 0)
    assert result.allow_discharge is True
    assert result.allow_charge is True
    assert result.feedback_w == -400
    assert result.request_released is False


@pytest.mark.parametrize(
    ("battery_w", "normalized"),
    [
        (24, 0.0),
        (24.999, 0.0),
        (-24, 0.0),
        (-24.999, 0.0),
        (25, 25.0),
        (-25, -25.0),
    ],
)
def test_battery_measurement_floor_boundaries(battery_w: float, normalized: float) -> None:
    assert normalize_battery_measurement(battery_w) == normalized


def test_battery_floor_changes_site_balance_target_and_feedback() -> None:
    """A 24 W measurement is idle; 25 W still enters N and F."""
    inside = known_ev_feedback(5400, 24, 7000)
    assert inside.site_balance_w == 5400
    assert inside.target_battery_w == 0
    assert inside.feedback_w == 0
    assert inside.allow_discharge is True
    assert inside.allow_charge is False

    above = known_ev_feedback(5400, 25, 7000)
    assert above.site_balance_w == 5425
    assert above.target_battery_w == 0
    assert above.feedback_w == -25

    negative_inside = known_ev_feedback(200, -24.999, 100)
    assert negative_inside.site_balance_w == 200
    assert negative_inside.target_battery_w == 100
    assert negative_inside.feedback_w == 100

    negative_floor = known_ev_feedback(5400, -25, 7000)
    assert negative_floor.site_balance_w == 5375
    assert negative_floor.target_battery_w == 0
    assert negative_floor.feedback_w == 25


def test_non_finite_battery_measurement_is_rejected() -> None:
    with pytest.raises(ValueError):
        known_ev_feedback(100, math.nan, 0)
    with pytest.raises(ValueError):
        normalize_battery_measurement(math.inf)


def test_feedback_type_does_not_default_to_released() -> None:
    result = EvExclusionFeedback(
        allow_discharge=False,
        allow_charge=False,
        feedback_w=None,
        target_battery_w=None,
        site_balance_w=None,
    )
    assert result.request_released is False
