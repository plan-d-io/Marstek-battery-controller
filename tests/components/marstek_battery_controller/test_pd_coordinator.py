"""Coordinator-level battery PD policy, without a Home Assistant runtime.

The session is the control core the coordinator calls. These tests cover the
operating rules, the write gate, and the deadband entity contract.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.marstek_battery_controller import const
from custom_components.marstek_battery_controller.battery_transport import deliver_command
from custom_components.marstek_battery_controller.calculator import compute_base
from custom_components.marstek_battery_controller.pd_controller import DEFAULT_DEADBAND_W
from custom_components.marstek_battery_controller.pd_session import (
    BatteryControlSession,
    ChannelSample,
    SessionSettings,
    normalize_pd_grid_deadband,
    reported_command_w,
)

_TZ = ZoneInfo("Europe/Brussels")
_ROOT = Path("custom_components/marstek_battery_controller/translations")


def _settings(**kwargs: object) -> SessionSettings:
    defaults: dict[str, object] = dict(
        mode=const.MODE_SELF_CONSUMPTION,
        soc=50.0,
        soc_sensor_ok=True,
        min_soc=12.0,
        max_soc=100.0,
        max_battery_power_w=2500.0,
        min_interval_s=5.0,
        deadband_w=0.0,
        reserve_target_soc=50.0,
        boost_charge_power_w=1250.0,
        manual_target_soc=80.0,
        manual_power_w=1000.0,
        reserve_protection_start=time(13, 0),
        peak_window_start=time(18, 0),
        capacity_tariff_enabled=True,
        cap_now=0.0,
        effective_cap_w=2500.0,
        latest_start_charge=const.LATEST_START_NO_NEED,
        peak_window_deadline=datetime(2026, 4, 27, 18, 0, tzinfo=_TZ),
        now=datetime(2026, 4, 27, 12, 0, tzinfo=_TZ),
    )
    defaults.update(kwargs)
    return SessionSettings(**defaults)  # type: ignore[arg-type]


def _grid(value: float, publication_id: str, source: float = 0.0) -> ChannelSample:
    return ChannelSample(value_w=value, publication_id=publication_id, source_mono=source)


def _battery(value: float, publication_id: str, source: float) -> ChannelSample:
    return ChannelSample(value_w=value, publication_id=publication_id, source_mono=source)


def _none_battery() -> ChannelSample:
    return ChannelSample(value_w=None, available=False)


def _plan(session: BatteryControlSession, settings: SessionSettings, **kwargs: object):
    grid = kwargs.pop("grid", _grid(1000.0, "g1", 0.0))
    battery = kwargs.pop("battery", _none_battery())
    return session.plan(
        settings,
        now_mono=float(kwargs.pop("now_mono", 0.0)),  # type: ignore[arg-type]
        grid=grid,  # type: ignore[arg-type]
        battery=battery,  # type: ignore[arg-type]
        reported_command_w=kwargs.pop("reported", 0),  # type: ignore[arg-type]
        opportunity=bool(kwargs.pop("opportunity", True)),
    )


def test_self_consumption_uses_pd_not_battery_plus_grid() -> None:
    session = BatteryControlSession()
    plan = _plan(session, _settings(), grid=_grid(1000.0, "g1", 0.0), reported=0)
    legacy = int(round(compute_base(1000.0, 0.0)))
    assert plan.proposed_w == 875
    assert plan.proposed_w != legacy
    assert plan.send_w == 875
    assert session.pd.requested_w == 875
    assert plan.operating_state == const.STATE_SELF_CONSUMPTION


def test_send_interval_is_a_minimum_gate_not_a_heartbeat() -> None:
    session = BatteryControlSession()
    settings = _settings(min_interval_s=5.0)
    first = _plan(session, settings, now_mono=0.0, grid=_grid(1000.0, "g1", 0.0))
    assert first.send_w == 875
    held = session.pd.requested_w
    blocked = _plan(
        session,
        settings,
        now_mono=1.0,
        grid=_grid(2000.0, "g2", 1.0),
        reported=0,
    )
    assert blocked.send_w is None
    assert session.pd.requested_w == held

    integrated = _plan(
        session,
        settings,
        now_mono=5.5,
        grid=_grid(2000.0, "g2", 1.0),
        reported=0,
    )
    assert session.pd.requested_w != held
    assert integrated.send_w == session.pd.requested_w

    quiet_session = BatteryControlSession()
    quiet_session.pd.requested_w = 400
    quiet_session.pd.last_opportunity_mono = 0.0
    quiet_session.ledger.accepted_w = 400
    quiet_session.ledger.last_success_mono = 0.0
    quiet = _plan(
        quiet_session,
        _settings(deadband_w=40.0, min_interval_s=5.0),
        now_mono=20.0,
        grid=_grid(0.0, "g-fresh", 20.0),
        battery=_battery(400.0, "b-ok", 20.0),
        reported=400,
    )
    assert quiet.send_w is None
    assert quiet_session.pd.requested_w == 400


def test_matching_reported_command_with_delivery_skips_the_write() -> None:
    session = BatteryControlSession()
    session.pd.requested_w = 400
    session.ledger.accepted_w = 400
    session.ledger.last_success_mono = 0.0
    plan = _plan(
        session,
        _settings(deadband_w=40.0),
        now_mono=100.0,
        grid=_grid(0.0, "g0", 100.0),
        battery=_battery(400.0, "b1", 100.0),
        reported=400,
    )
    assert plan.proposed_w == 400
    assert plan.send_w is None


def test_persistent_non_delivery_reasserts_then_reanchors() -> None:
    session = BatteryControlSession()
    session.pd.requested_w = 400
    session.ledger.accepted_w = 400
    session.ledger.last_success_mono = 0.0
    settings = _settings(deadband_w=40.0)
    first = _plan(
        session,
        settings,
        now_mono=100.0,
        grid=_grid(0.0, "g0", 100.0),
        battery=_battery(0.0, "b1", 100.0),
        reported=400,
    )
    assert first.send_w == 400
    assert first.reassert
    assert session.pd.requested_w == 400
    assert session.ledger.accepted_w == 400

    _plan(
        session,
        settings,
        now_mono=100.2,
        grid=_grid(0.0, "g0", 100.0),
        battery=_battery(0.0, "b2", 100.2),
        reported=400,
    )
    third = _plan(
        session,
        settings,
        now_mono=100.4,
        grid=_grid(0.0, "g0", 100.0),
        battery=_battery(0.0, "b3", 100.4),
        reported=400,
    )
    assert session.pd.requested_w == 0
    assert session.ledger.accepted_w == 400
    assert third.send_w == 0
    assert third.safety


def test_stale_grid_issues_safety_zero_without_advancing_pd() -> None:
    session = BatteryControlSession()
    first = _plan(session, _settings(), now_mono=0.0, grid=_grid(1000.0, "g1", 0.0))
    filtered = session.pd.grid_filtered
    requested = session.pd.requested_w
    integrated = session.pd.last_integrate_mono
    stale = _plan(
        session,
        _settings(),
        now_mono=30.0,
        grid=_grid(1000.0, "g1", 0.0),
        reported=first.proposed_w,
    )
    assert stale.proposed_w == 0
    assert stale.send_w == 0
    assert stale.safety
    assert session.pd.requested_w == requested
    assert session.pd.grid_filtered == filtered
    assert session.pd.last_integrate_mono == integrated


def test_minimum_and_maximum_soc_are_permissions() -> None:
    floor = BatteryControlSession()
    blocked = _plan(
        floor,
        _settings(soc=12.0, min_soc=12.0),
        grid=_grid(2000.0, "gf", 0.0),
    )
    assert blocked.proposed_w == 0
    assert blocked.reason_code == const.REASON_AT_FLOOR
    charging = _plan(
        floor,
        _settings(soc=12.0, min_soc=12.0),
        now_mono=10.0,
        grid=_grid(-2000.0, "gc", 10.0),
    )
    assert charging.proposed_w < 0
    assert charging.reason_code == const.REASON_NORMAL

    ceiling = BatteryControlSession()
    no_charge = _plan(
        ceiling,
        _settings(soc=100.0, max_soc=100.0),
        grid=_grid(-2000.0, "gx", 0.0),
    )
    assert no_charge.proposed_w == 0
    assert no_charge.reason_code == const.REASON_AT_CEILING
    discharging = _plan(
        ceiling,
        _settings(soc=100.0, max_soc=100.0),
        now_mono=10.0,
        grid=_grid(2000.0, "gy", 10.0),
    )
    assert discharging.proposed_w > 0
    assert discharging.reason_code == const.REASON_NORMAL


def test_inactive_boost_uses_pd() -> None:
    session = BatteryControlSession()
    plan = _plan(
        session,
        _settings(mode=const.MODE_SELF_CONSUMPTION_BOOST),
        grid=_grid(1000.0, "g1", 0.0),
    )
    assert plan.send_w == 875
    assert plan.send_w != -1250
    assert plan.reason_code == const.REASON_NORMAL
    assert plan.operating_state == const.STATE_SELF_CONSUMPTION


def test_active_boost_uses_fixed_charge_and_freezes_pd() -> None:
    session = BatteryControlSession()
    session.pd.requested_w = 800
    session.pd.last_integrate_mono = 3.0
    settings = _settings(
        mode=const.MODE_SELF_CONSUMPTION_BOOST,
        latest_start_charge=datetime(2026, 4, 27, 14, 0, tzinfo=_TZ),
        now=datetime(2026, 4, 27, 15, 0, tzinfo=_TZ),
    )
    plan = _plan(session, settings, now_mono=50.0, grid=_grid(1000.0, "g1", 50.0))
    assert plan.send_w == -1250
    assert plan.reason_code == const.REASON_BOOST_ACTIVE
    assert plan.operating_state == const.STATE_BOOST_CHARGING
    assert session.pd.requested_w == 800
    assert session.pd.last_integrate_mono == 3.0
    _plan(session, settings, now_mono=51.0, grid=_grid(2000.0, "g2", 51.0))
    assert session.pd.requested_w == 800
    assert session.pd.last_integrate_mono == 3.0


def test_leaving_boost_reanchors_before_pd_resumes() -> None:
    session = BatteryControlSession()
    session.pd.requested_w = 800
    session.kind = "boost_fixed"
    session.ledger.pending_w = -1250
    resumed = _plan(
        session,
        _settings(mode=const.MODE_SELF_CONSUMPTION, deadband_w=40.0),
        now_mono=80.0,
        grid=_grid(0.0, "g0", 80.0),
        battery=_battery(-400.0, "b-exit", 80.0),
        reported=-1250,
    )
    assert session.ledger.pending_w is None
    assert session.pd.requested_w == -400
    assert resumed.proposed_w == -400
    assert resumed.proposed_w != 800
    assert resumed.send_w != -1250


def test_boost_blocked_by_capacity_tariff_falls_back_to_pd() -> None:
    session = BatteryControlSession()
    plan = _plan(
        session,
        _settings(
            mode=const.MODE_SELF_CONSUMPTION_BOOST,
            latest_start_charge=datetime(2026, 4, 27, 14, 0, tzinfo=_TZ),
            now=datetime(2026, 4, 27, 15, 0, tzinfo=_TZ),
            cap_now=3000.0,
            effective_cap_w=2500.0,
        ),
        grid=_grid(1000.0, "g1", 0.0),
    )
    assert plan.reason_code == const.REASON_CAP_TARIFF
    assert plan.operating_state == const.STATE_SELF_CONSUMPTION
    assert plan.send_w == 875
    assert plan.send_w != -1250


def test_reserve_normally_uses_pd() -> None:
    session = BatteryControlSession()
    plan = _plan(
        session,
        _settings(mode=const.MODE_SELF_CONSUMPTION_RESERVE, soc=40.0),
        grid=_grid(1000.0, "g1", 0.0),
    )
    assert plan.send_w == 875
    assert plan.operating_state == const.STATE_SELF_CONSUMPTION
    assert plan.reason_code == const.REASON_NORMAL


def test_reserve_protection_blocks_discharge_and_allows_pv_charge() -> None:
    settings = _settings(
        mode=const.MODE_SELF_CONSUMPTION_RESERVE,
        soc=40.0,
        reserve_target_soc=50.0,
        now=datetime(2026, 4, 27, 15, 0, tzinfo=_TZ),
    )
    held = BatteryControlSession()
    blocked = _plan(held, settings, grid=_grid(2000.0, "gd", 0.0))
    assert blocked.proposed_w == 0
    assert blocked.reason_code == const.REASON_RESERVE_HELD
    assert blocked.operating_state == const.STATE_RESERVE_HELD

    charging = BatteryControlSession()
    allowed = _plan(charging, settings, grid=_grid(-2000.0, "gc", 0.0))
    assert allowed.proposed_w < 0
    assert allowed.operating_state == const.STATE_SELF_CONSUMPTION


def test_reserve_capacity_exception_permits_discharge() -> None:
    session = BatteryControlSession()
    plan = _plan(
        session,
        _settings(
            mode=const.MODE_SELF_CONSUMPTION_RESERVE,
            soc=40.0,
            reserve_target_soc=50.0,
            now=datetime(2026, 4, 27, 15, 0, tzinfo=_TZ),
            cap_now=3000.0,
            effective_cap_w=2500.0,
        ),
        grid=_grid(2000.0, "g1", 0.0),
    )
    assert plan.proposed_w > 0
    assert plan.reason_code == const.REASON_CAP_TARIFF
    assert plan.operating_state == const.STATE_SELF_CONSUMPTION


def test_manual_bypasses_pd_and_released_clears_queued_commands() -> None:
    session = BatteryControlSession()
    session.kind = "pd"
    session.pd.requested_w = 500
    session.ledger.pending_w = 700
    session.ledger.in_flight_w = 100
    manual = _plan(
        session,
        _settings(mode=const.MODE_MANUAL, soc=40.0, manual_target_soc=80.0),
        grid=_grid(1000.0, "g1", 0.0),
    )
    assert manual.send_w == -1000
    assert manual.reason_code == const.REASON_MANUAL_ACTIVE
    assert session.pd.requested_w == 500
    assert session.ledger.pending_w is None
    assert session.ledger.in_flight_w == 100

    session.ledger.pending_w = 700
    session.pd.requested_w = 500
    released = _plan(session, _settings(mode=const.MODE_RELEASED))
    assert released.release
    assert released.send_w is None
    assert released.reason_code == const.REASON_RELEASED
    assert session.pd.requested_w == 0
    assert session.ledger.pending_w is None
    assert session.ledger.in_flight_w == 100


def test_failed_write_does_not_accept_the_proposal() -> None:
    session = BatteryControlSession()
    plan = _plan(session, _settings(), grid=_grid(1000.0, "g1", 0.0))
    assert plan.send_w == 875

    class _Down:
        async def async_send_command(self, command_w: int) -> None:
            raise RuntimeError("modbus down")

        async def async_release(self) -> None:
            return None

    result = asyncio.run(
        deliver_command(_Down(), session.ledger, plan.send_w, 0.0, pd_state=session.pd)
    )
    assert result.accepted is False
    assert session.ledger.accepted_w == 0
    assert session.pd.requested_w == 875
    assert session.pd.last_direction == 0


def test_successful_write_accepts_the_sent_command() -> None:
    session = BatteryControlSession()
    plan = _plan(session, _settings(), grid=_grid(1000.0, "g1", 0.0))

    class _Ok:
        async def async_send_command(self, command_w: int) -> None:
            return None

        async def async_release(self) -> None:
            return None

    result = asyncio.run(
        deliver_command(_Ok(), session.ledger, plan.send_w, 0.0, pd_state=session.pd)
    )
    assert result.accepted
    assert result.sent_w == 875
    assert session.ledger.accepted_w == 875
    assert session.pd.last_direction == 1


@pytest.mark.parametrize(
    ("mode", "charge", "discharge", "expected"),
    [
        ("stop", 400, 500, 0),
        ("standby", 400, 500, 0),
        ("neutral", 10, 10, 0),
        ("discharge", 100, 400, 400),
        ("charge", 350, 100, -350),
    ],
)
def test_reported_command_reconstruction(
    mode: str,
    charge: float,
    discharge: float,
    expected: int,
) -> None:
    assert reported_command_w(mode, charge, discharge) == expected


def test_unknown_force_mode_is_not_adopted() -> None:
    assert reported_command_w(None, 10, 10) is None
    assert reported_command_w("auto", 10, 10) is None
    assert reported_command_w("charge", None, 400) is None
    assert reported_command_w("discharge", 400, None) is None


def test_stale_soc_sends_safety_zero_without_advancing_pd() -> None:
    """A stale SoC must not keep a frozen charge or discharge permission."""
    from custom_components.marstek_battery_controller.pd_session import ReportedObservation

    session = BatteryControlSession()
    session.pd.requested_w = 400
    session.pd.grid_filtered_w = 1000.0
    session.pd.last_integrate_mono = 3.0
    session.kind = "pd"
    plan = _plan(
        session,
        _settings(soc_fresh=False),
        now_mono=20.0,
        reported=400,
        battery=_battery(400.0, "b1", 20.0),
    )
    assert plan.proposed_w == 0
    assert plan.send_w == 0
    assert plan.safety is True
    assert plan.release is False
    assert session.pd.requested_w == 400
    assert session.pd.last_integrate_mono == 3.0
    stale = ReportedObservation(
        command_w=0,
        publication_id="old",
        observed_mono=1.0,
        fresh=False,
        coherent=True,
    )
    session.ledger.last_success_mono = 10.0
    session.ledger.accepted_w = 400
    assert session.consider_reported(stale, now_mono=20.0) is False
    assert session.ledger.accepted_w == 400
    older = ReportedObservation(
        command_w=0,
        publication_id="older-than-write",
        observed_mono=9.0,
        fresh=True,
        coherent=True,
    )
    assert session.consider_reported(older, now_mono=20.0) is False
    assert session.ledger.accepted_w == 400
    assert session.pd.last_direction == 1
    external = ReportedObservation(
        command_w=-250,
        publication_id="external",
        observed_mono=11.0,
        fresh=True,
        coherent=True,
    )
    assert session.consider_reported(external, now_mono=20.0) is True
    assert session.ledger.accepted_w == -250
    assert session.pd.last_direction == -1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (40, 40.0),
        (0, 0.0),
        (200, 200.0),
        (201, 200.0),
        (-5, 0.0),
        (42, 40.0),
        (37, 35.0),
    ],
)
def test_pd_grid_deadband_entity_bounds(raw: float, expected: float) -> None:
    assert DEFAULT_DEADBAND_W == 40.0
    assert normalize_pd_grid_deadband(raw) == expected


def _flatten(value: object, prefix: str = "") -> dict[str, object]:
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten(item, child))
        return out
    return {prefix: value}


def test_translation_parity_includes_pd_grid_deadband() -> None:
    english = json.loads((_ROOT / "en.json").read_text(encoding="utf-8"))
    english_keys = set(_flatten(english))
    assert "entity.number.pd_grid_deadband.name" in english_keys
    assert "config.step.parameters.data.pd_grid_deadband" in english_keys
    assert "config.step.init.data.pd_grid_deadband" in english_keys
    for path in sorted(_ROOT.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        keys = set(_flatten(payload))
        assert keys == english_keys, path.name
        assert str(payload["entity"]["number"]["pd_grid_deadband"]["name"]).strip()
