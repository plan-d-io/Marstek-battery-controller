"""EV exclusion inside BatteryControlSession, without Home Assistant."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time
from zoneinfo import ZoneInfo

from custom_components.marstek_battery_controller import const
from custom_components.marstek_battery_controller.pd_session import (
    BatteryControlSession,
    ChannelSample,
    SessionSettings,
)

_TZ = ZoneInfo("Europe/Brussels")


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


def _sample(
    value: float | None,
    publication_id: str,
    source: float,
    *,
    available: bool = True,
) -> ChannelSample:
    return ChannelSample(
        value_w=value,
        publication_id=publication_id,
        source_mono=source,
        available=available,
    )


def _plan(
    session: BatteryControlSession,
    settings: SessionSettings,
    *,
    now: float,
    grid: ChannelSample,
    battery: ChannelSample | None = None,
    ev: ChannelSample | None = None,
    reported: int | None = 0,
) -> object:
    if battery is None:
        battery = ChannelSample(value_w=None, available=False)
    return session.plan(
        settings,
        now_mono=now,
        grid=grid,
        battery=battery,
        ev=ev,
        reported_command_w=reported,
    )


def _snapshot(session: BatteryControlSession) -> dict[str, object]:
    pd = session.pd
    return {
        "requested_w": pd.requested_w,
        "last_direction": pd.last_direction,
        "grid_filtered": pd.grid_filtered,
        "zero_cross_holding": pd.zero_cross_holding,
        "opposite_since_mono": pd.opposite_since_mono,
        "last_opportunity_mono": pd.last_opportunity_mono,
        "last_integrate_mono": pd.last_integrate_mono,
        "accepted_w": session.ledger.accepted_w,
        "in_flight_w": session.ledger.in_flight_w,
    }


def test_exclude_ev_false_matches_the_raw_grid_path() -> None:
    grid = _sample(1000.0, "g-raw", 0.0)
    ev = _sample(7000.0, "e-ignored", 0.0)
    battery = _sample(0.0, "b0", 0.0)
    plain = BatteryControlSession()
    explicit = BatteryControlSession()
    with_ev = BatteryControlSession()
    settings = _settings()
    off = _settings(exclude_ev=False)
    first = _plan(plain, settings, now=0.0, grid=grid, battery=battery)
    second = _plan(explicit, off, now=0.0, grid=grid, battery=battery)
    third = _plan(with_ev, off, now=0.0, grid=grid, battery=battery, ev=ev)

    assert first.proposed_w == second.proposed_w == third.proposed_w
    assert first.send_w == third.send_w
    assert plain.pd.requested_w == with_ev.pd.requested_w
    assert with_ev.pd.last_publication_id == "g-raw"
    assert with_ev.publications.ev.value == 7000.0
    assert plain._feedback_basis == "raw"


def test_ev_only_import_leaves_the_battery_idle() -> None:
    excluded = BatteryControlSession()
    raw = BatteryControlSession()
    grid = _sample(7000.0, "g", 0.0)
    battery = _sample(0.0, "b", 0.0)
    ev = _sample(7000.0, "e", 0.0)
    plan = _plan(
        excluded,
        _settings(exclude_ev=True),
        now=0.0,
        grid=grid,
        battery=battery,
        ev=ev,
    )
    untouched = _plan(raw, _settings(), now=0.0, grid=grid, battery=battery)

    assert plan.proposed_w == 0
    assert excluded.pd.requested_w == 0
    assert untouched.proposed_w > 0
    assert plan.release is False


def test_house_plus_ev_import_covers_only_the_house() -> None:
    excluded = BatteryControlSession()
    house_only = BatteryControlSession()
    raw = BatteryControlSession()
    battery = _sample(0.0, "b", 0.0)
    ev = _sample(7000.0, "e", 0.0)
    adjusted = _plan(
        excluded,
        _settings(exclude_ev=True),
        now=0.0,
        grid=_sample(8200.0, "g", 0.0),
        battery=battery,
        ev=ev,
    )
    reference = _plan(
        house_only,
        _settings(),
        now=0.0,
        grid=_sample(1200.0, "g-house", 0.0),
        battery=battery,
    )
    whole_grid = _plan(
        raw,
        _settings(),
        now=0.0,
        grid=_sample(8200.0, "g-all", 0.0),
        battery=battery,
    )

    assert adjusted.proposed_w == reference.proposed_w
    assert adjusted.proposed_w > 0
    assert adjusted.proposed_w < whole_grid.proposed_w
    assert excluded.pd.last_publication_id is not None
    assert excluded.pd.last_publication_id.startswith("ev-known|")
    assert "g-all" not in excluded.pd.last_publication_id


def test_pv_covering_the_house_and_part_of_the_ev_stays_idle() -> None:
    session = BatteryControlSession()
    plan = _plan(
        session,
        _settings(exclude_ev=True),
        now=0.0,
        grid=_sample(4500.0, "g", 0.0),
        battery=_sample(0.0, "b", 0.0),
        ev=_sample(7000.0, "e", 0.0),
    )
    assert plan.proposed_w == 0
    assert session.pd.requested_w == 0
    assert plan.release is False


def test_physical_export_charges_only_the_actual_export() -> None:
    excluded = BatteryControlSession()
    export = BatteryControlSession()
    battery = _sample(0.0, "b", 0.0)
    ev = _sample(7000.0, "e", 0.0)
    adjusted = _plan(
        excluded,
        _settings(exclude_ev=True),
        now=0.0,
        grid=_sample(-2500.0, "g", 0.0),
        battery=battery,
        ev=ev,
    )
    reference = _plan(
        export,
        _settings(),
        now=0.0,
        grid=_sample(-2500.0, "g-export", 0.0),
        battery=battery,
    )

    assert adjusted.proposed_w == reference.proposed_w
    assert adjusted.proposed_w < 0
    assert excluded.pd.last_publication_id is not None
    assert excluded.pd.last_publication_id.startswith("ev-known|")


def test_existing_discharge_into_the_ev_slews_down_and_does_not_rebuild() -> None:
    session = BatteryControlSession()
    session.pd.requested_w = 2000
    session.pd.last_direction = 1
    session.pd.last_integrate_mono = 0.0
    session.pd.last_opportunity_mono = 0.0
    settings = _settings(exclude_ev=True, min_interval_s=1.0)
    previous = 2000
    for step in range(1, 12):
        plan = _plan(
            session,
            settings,
            now=float(step),
            grid=_sample(5000.0, f"g{step}", float(step)),
            battery=_sample(2000.0, f"b{step}", float(step)),
            ev=_sample(7000.0, "e", 1.0),
        )
        assert plan.release is False
        assert session.pd.requested_w <= previous
        previous = session.pd.requested_w
        if step == 1:
            assert 1200 < session.pd.requested_w < 2000
        if session.pd.requested_w == 0:
            break
    else:
        raise AssertionError("discharge did not unwind")

    idle = _plan(
        session,
        settings,
        now=20.0,
        grid=_sample(7000.0, "g-idle", 20.0),
        battery=_sample(0.0, "b-idle", 20.0),
        ev=_sample(7000.0, "e", 1.0),
    )
    assert idle.proposed_w == 0
    assert session.pd.requested_w == 0
    assert idle.release is False


def test_known_loss_of_surplus_stops_charging_immediately() -> None:
    session = BatteryControlSession()
    session.pd.requested_w = -1500
    session.pd.last_direction = -1
    session.pd.grid_filtered = -800.0
    session.pd.last_opportunity_mono = 10.0
    session.pd.last_integrate_mono = 10.0
    session.ledger.accepted_w = -1500
    plan = _plan(
        session,
        _settings(exclude_ev=True, min_interval_s=5.0),
        now=10.2,
        grid=_sample(2500.0, "g", 10.2),
        battery=_sample(-1500.0, "b", 10.2),
        ev=_sample(2000.0, "e", 10.2),
        reported=-1500,
    )

    assert plan.proposed_w == 0
    assert plan.release is False
    assert session.pd.requested_w == 0
    assert session.pd.last_direction == -1
    assert session.pd.grid_filtered == -800.0
    assert session.pd.last_opportunity_mono == 10.0
    assert session.pd.last_integrate_mono == 10.0
    assert session.ledger.accepted_w == -1500


def test_ev_normalized_to_zero_matches_raw_grid() -> None:
    battery = ChannelSample(value_w=None, available=False)
    for ev_w, publication_id in ((0.0, "e0"), (24.0, "e24")):
        excluded = BatteryControlSession()
        raw = BatteryControlSession()
        grid = _sample(1000.0, "g0", 0.0)
        adjusted = _plan(
            excluded,
            _settings(exclude_ev=True),
            now=0.0,
            grid=grid,
            battery=battery,
            ev=_sample(ev_w, publication_id, 0.0),
        )
        reference = _plan(raw, _settings(), now=0.0, grid=grid, battery=battery)
        assert adjusted.proposed_w == reference.proposed_w
        assert excluded.pd.requested_w == raw.pd.requested_w
        assert excluded._feedback_basis == "ev_zero"
        identity = excluded.pd.last_publication_id
        assert identity is not None
        assert identity.startswith("ev-zero|")
        assert identity != "g0"
        assert f"id:{publication_id}" in identity
        assert "id:g0" in identity
        assert "battery=" not in identity


def test_unusable_ev_on_import_closes_both_directions_without_release() -> None:
    raw = BatteryControlSession()
    raw_plan = _plan(
        raw,
        _settings(),
        now=0.0,
        grid=_sample(1500.0, "g-raw", 0.0),
    )
    assert raw_plan.proposed_w > 0

    for ev in (
        None,
        ChannelSample(value_w=None, available=False),
        _sample(-500.0, "e-neg", 0.0),
    ):
        for requested in (600, -400):
            session = BatteryControlSession()
            session.pd.requested_w = requested
            session.pd.last_direction = 1 if requested > 0 else -1
            plan = _plan(
                session,
                _settings(exclude_ev=True),
                now=0.0,
                grid=_sample(1500.0, "g", 0.0),
                ev=ev,
            )
            assert plan.proposed_w == 0
            assert plan.release is False
            assert plan.reason_code != const.REASON_RELEASED
            assert session.kind == "pd"
            assert session.pd.requested_w == 0


def test_unusable_ev_on_fresh_export_still_permits_charging() -> None:
    for ev in (None, _sample(-500.0, "e-neg", 0.0)):
        excluded = BatteryControlSession()
        raw = BatteryControlSession()
        adjusted = _plan(
            excluded,
            _settings(exclude_ev=True),
            now=0.0,
            grid=_sample(-1800.0, "g-export", 0.0),
            ev=ev,
        )
        reference = _plan(
            raw,
            _settings(),
            now=0.0,
            grid=_sample(-1800.0, "g-export", 0.0),
        )
        assert adjusted.proposed_w == reference.proposed_w
        assert adjusted.proposed_w < 0
        assert adjusted.release is False
        assert excluded.kind == "pd"
        assert excluded.pd.last_publication_id is not None
        assert excluded.pd.last_publication_id.startswith("ev-guard|")
        assert excluded.pd.last_publication_id != "g-export"


def test_positive_ev_without_usable_battery_uses_the_guarded_policy() -> None:
    unavailable = BatteryControlSession()
    unavailable.pd.requested_w = 500
    blocked = _plan(
        unavailable,
        _settings(exclude_ev=True),
        now=0.0,
        grid=_sample(2000.0, "g", 0.0),
        battery=ChannelSample(value_w=None, available=False),
        ev=_sample(4000.0, "e", 0.0),
    )
    assert blocked.proposed_w == 0
    assert blocked.release is False
    assert unavailable.pd.requested_w == 0

    stale = BatteryControlSession()
    stale.pd.requested_w = 500
    stale_plan = _plan(
        stale,
        _settings(exclude_ev=True),
        now=30.0,
        grid=_sample(2000.0, "g-stale", 30.0),
        battery=_sample(100.0, "b-old", 0.0),
        ev=_sample(4000.0, "e-stale", 0.0),
    )
    assert stale_plan.proposed_w == 0
    assert stale_plan.release is False
    assert stale.pd.requested_w == 0
    assert stale.publications.ev_is_usable()

    excluded = BatteryControlSession()
    raw = BatteryControlSession()
    export = _plan(
        excluded,
        _settings(exclude_ev=True),
        now=30.0,
        grid=_sample(-1800.0, "g-export", 30.0),
        battery=_sample(100.0, "b-old", 0.0),
        ev=_sample(4000.0, "e-export", 0.0),
    )
    reference = _plan(
        raw,
        _settings(),
        now=30.0,
        grid=_sample(-1800.0, "g-export", 30.0),
    )
    assert export.proposed_w == reference.proposed_w
    assert export.proposed_w < 0
    assert export.release is False
    assert excluded.pd.last_publication_id is not None
    assert excluded.pd.last_publication_id.startswith("ev-guard|")


def test_newer_ev_publication_integrates_against_the_same_grid_once_the_gate_opens() -> None:
    session = BatteryControlSession()
    settings = _settings(exclude_ev=True, min_interval_s=5.0)
    battery = _sample(0.0, "b1", 0.0)
    first = _plan(
        session,
        settings,
        now=0.0,
        grid=_sample(8200.0, "g1", 0.0),
        battery=battery,
        ev=_sample(7000.0, "e1", 0.0),
    )
    first_request = session.pd.requested_w
    first_identity = session.pd.last_publication_id
    assert first.proposed_w > 0
    assert first_identity is not None
    assert first_identity.startswith("ev-known|")
    assert session.publications.grid.publication_id == "g1"

    held = _plan(
        session,
        settings,
        now=1.0,
        grid=_sample(8200.0, "g1", 0.0),
        battery=_sample(0.0, "b2", 1.0),
        ev=_sample(2000.0, "e2", 1.0),
    )
    assert held.send_w is None
    assert session.pd.requested_w == first_request
    assert session.pd.last_publication_id == first_identity

    integrated = _plan(
        session,
        settings,
        now=5.0,
        grid=_sample(8200.0, "g1", 0.0),
        battery=_sample(0.0, "b2", 1.0),
        ev=_sample(2000.0, "e2", 1.0),
    )
    assert integrated.proposed_w > first_request
    assert session.pd.requested_w > first_request
    assert session.pd.last_publication_id != first_identity
    assert session.pd.last_publication_id is not None
    assert session.pd.last_publication_id.startswith("ev-known|")
    assert session.publications.grid.publication_id == "g1"
    assert "id:e2" in session.pd.last_publication_id


def test_repeating_the_same_composite_publication_does_not_integrate_twice() -> None:
    session = BatteryControlSession()
    settings = _settings(exclude_ev=True, min_interval_s=1.0)
    grid = _sample(8200.0, "g1", 0.0)
    battery = _sample(0.0, "b1", 0.0)
    ev = _sample(7000.0, "e1", 0.0)
    _plan(session, settings, now=0.0, grid=grid, battery=battery, ev=ev)
    requested = session.pd.requested_w
    identity = session.pd.last_publication_id
    integrated_at = session.pd.last_integrate_mono

    repeat = _plan(session, settings, now=1.0, grid=grid, battery=battery, ev=ev)
    assert repeat.proposed_w == requested
    assert session.pd.requested_w == requested
    assert session.pd.last_publication_id == identity
    assert session.pd.last_integrate_mono == integrated_at
    assert session.publications.grid.publication_id == "g1"


def test_stable_ev_power_remains_usable_after_a_long_elapsed_time() -> None:
    session = BatteryControlSession()
    settings = _settings(exclude_ev=True)
    ev = _sample(7000.0, "e-stable", 0.0)
    _plan(
        session,
        settings,
        now=0.0,
        grid=_sample(8200.0, "g0", 0.0),
        battery=_sample(0.0, "b0", 0.0),
        ev=ev,
    )
    later = _plan(
        session,
        settings,
        now=10000.0,
        grid=_sample(8200.0, "g-late", 10000.0),
        battery=_sample(0.0, "b-late", 10000.0),
        ev=ev,
    )
    age = session.publications.ev.age_s(10000.0)
    assert session.publications.ev_is_usable()
    assert session.publications.ev.value == 7000.0
    assert age is not None and age > 1000.0
    assert later.proposed_w > 0
    assert later.release is False
    assert session._feedback_basis == "known_ev"


def test_enabling_and_disabling_exclusion_changes_basis_without_resetting() -> None:
    session = BatteryControlSession()
    settings = _settings(min_interval_s=5.0)
    _plan(
        session,
        settings,
        now=0.0,
        grid=_sample(1000.0, "g0", 0.0),
        battery=_sample(0.0, "b0", 0.0),
    )
    assert session._feedback_basis == "raw"
    session.pd.requested_w = 800
    session.pd.last_direction = 1
    session.pd.grid_filtered = 400.0
    session.pd.zero_cross_holding = True
    session.pd.opposite_since_mono = 0.25
    session.pd.last_opportunity_mono = 0.0
    session.pd.last_integrate_mono = 0.0
    session.pd.last_publication_id = "g0"
    session.pd.last_source_mono = 0.0
    session.ledger.accepted_w = 800
    before = _snapshot(session)

    enabled = _plan(
        session,
        _settings(exclude_ev=True, min_interval_s=5.0),
        now=1.0,
        grid=_sample(8200.0, "g1", 1.0),
        battery=_sample(0.0, "b1", 1.0),
        ev=_sample(7000.0, "e0", 1.0),
        reported=800,
    )
    assert session._feedback_basis == "known_ev"
    assert session.pd.last_publication_id is None
    assert session.pd.last_source_mono is None
    assert _snapshot(session) == before
    assert enabled.release is False

    session.pd.last_publication_id = "keep"
    session.pd.last_source_mono = 1.0
    _plan(
        session,
        _settings(exclude_ev=True, min_interval_s=5.0),
        now=2.0,
        grid=_sample(8200.0, "g1", 1.0),
        battery=_sample(0.0, "b1", 1.0),
        ev=_sample(7000.0, "e0", 1.0),
        reported=800,
    )
    assert session.pd.last_publication_id == "keep"
    assert session.pd.last_source_mono == 1.0
    assert _snapshot(session) == before

    _plan(
        session,
        _settings(exclude_ev=False, min_interval_s=5.0),
        now=3.0,
        grid=_sample(8200.0, "g1", 1.0),
        battery=_sample(0.0, "b1", 1.0),
        ev=_sample(7000.0, "e0", 1.0),
        reported=800,
    )
    assert session._feedback_basis == "raw"
    assert session.pd.last_publication_id is None
    assert session.pd.last_source_mono is None
    assert _snapshot(session) == before
    assert session.kind == "pd"


def test_fixed_boost_manual_released_and_stale_soc_ignore_exclusion() -> None:
    boost_on = _settings(
        mode=const.MODE_SELF_CONSUMPTION_BOOST,
        latest_start_charge=datetime(2026, 4, 27, 14, 0, tzinfo=_TZ),
        now=datetime(2026, 4, 27, 15, 0, tzinfo=_TZ),
        exclude_ev=True,
    )
    charging = BatteryControlSession()
    charging.pd.requested_w = 800
    charging.pd.last_integrate_mono = 3.0
    boost = _plan(
        charging,
        boost_on,
        now=50.0,
        grid=_sample(8200.0, "g", 50.0),
        battery=_sample(0.0, "b", 50.0),
        ev=_sample(7000.0, "e", 50.0),
    )
    assert boost.send_w == -1250
    assert boost.reason_code == const.REASON_BOOST_ACTIVE
    assert boost.release is False
    assert charging.pd.requested_w == 800
    assert charging.pd.last_integrate_mono == 3.0

    manual_session = BatteryControlSession()
    manual_session.pd.requested_w = 500
    manual = _plan(
        manual_session,
        _settings(mode=const.MODE_MANUAL, soc=40.0, manual_target_soc=80.0, exclude_ev=True),
        now=0.0,
        grid=_sample(8200.0, "g", 0.0),
        ev=_sample(7000.0, "e", 0.0),
    )
    assert manual.send_w == -1000
    assert manual.reason_code == const.REASON_MANUAL_ACTIVE
    assert manual.release is False
    assert manual_session.pd.requested_w == 500

    baseline = BatteryControlSession()
    excluded = BatteryControlSession()
    released_off = _plan(baseline, _settings(mode=const.MODE_RELEASED), now=0.0, grid=_sample(1000.0, "g", 0.0))
    released_on = _plan(
        excluded,
        _settings(mode=const.MODE_RELEASED, exclude_ev=True),
        now=0.0,
        grid=_sample(1000.0, "g", 0.0),
        ev=_sample(7000.0, "e", 0.0),
    )
    assert released_off.release is True
    assert released_on.release is True
    assert released_on.reason_code == const.REASON_RELEASED
    assert released_on.send_w is None
    assert excluded.pd.requested_w == baseline.pd.requested_w == 0

    stale = BatteryControlSession()
    stale.pd.requested_w = 400
    stale.pd.grid_filtered = 1000.0
    stale.pd.last_integrate_mono = 3.0
    safety = _plan(
        stale,
        _settings(soc_fresh=False, exclude_ev=True),
        now=20.0,
        grid=_sample(1500.0, "g", 20.0),
        battery=_sample(400.0, "b", 20.0),
        ev=None,
        reported=400,
    )
    assert safety.proposed_w == 0
    assert safety.send_w == 0
    assert safety.safety is True
    assert safety.release is False
    assert stale.pd.requested_w == 400
    assert stale.pd.last_integrate_mono == 3.0
    assert stale.pd.grid_filtered == 1000.0


def test_reserve_and_pd_fallback_boost_use_exclusion() -> None:
    ev_only_grid = _sample(7000.0, "g", 0.0)
    ev_only = _sample(7000.0, "e", 0.0)
    battery = _sample(0.0, "b", 0.0)
    inactive = BatteryControlSession()
    inactive_raw = BatteryControlSession()
    excluded_idle = _plan(
        inactive,
        _settings(mode=const.MODE_SELF_CONSUMPTION_BOOST, exclude_ev=True),
        now=0.0,
        grid=ev_only_grid,
        battery=battery,
        ev=ev_only,
    )
    raw_idle = _plan(
        inactive_raw,
        _settings(mode=const.MODE_SELF_CONSUMPTION_BOOST),
        now=0.0,
        grid=ev_only_grid,
        battery=battery,
    )
    assert excluded_idle.proposed_w == 0
    assert excluded_idle.reason_code == const.REASON_NORMAL
    assert raw_idle.proposed_w > 0

    fallback = BatteryControlSession()
    fallback_raw = BatteryControlSession()
    blocked_boost = _settings(
        mode=const.MODE_SELF_CONSUMPTION_BOOST,
        latest_start_charge=datetime(2026, 4, 27, 14, 0, tzinfo=_TZ),
        now=datetime(2026, 4, 27, 15, 0, tzinfo=_TZ),
        cap_now=3000.0,
        effective_cap_w=2500.0,
    )
    excluded_fallback = _plan(
        fallback,
        replace(blocked_boost, exclude_ev=True),
        now=0.0,
        grid=ev_only_grid,
        battery=battery,
        ev=ev_only,
    )
    raw_fallback = _plan(
        fallback_raw,
        blocked_boost,
        now=0.0,
        grid=ev_only_grid,
        battery=battery,
    )
    assert excluded_fallback.proposed_w == 0
    assert excluded_fallback.reason_code == const.REASON_CAP_TARIFF
    assert excluded_fallback.send_w != -1250
    assert raw_fallback.proposed_w > 0

    reserve = BatteryControlSession()
    reserve_raw = BatteryControlSession()
    reserve_settings = _settings(mode=const.MODE_SELF_CONSUMPTION_RESERVE, soc=60.0)
    house = _sample(0.0, "b-house", 0.0)
    adjusted = _plan(
        reserve,
        replace(reserve_settings, exclude_ev=True),
        now=0.0,
        grid=_sample(8200.0, "g", 0.0),
        battery=house,
        ev=_sample(7000.0, "e", 0.0),
    )
    whole = _plan(
        reserve_raw,
        reserve_settings,
        now=0.0,
        grid=_sample(8200.0, "g", 0.0),
        battery=house,
    )
    assert adjusted.proposed_w > 0
    assert adjusted.proposed_w < whole.proposed_w
    assert reserve._feedback_basis == "known_ev"


def test_soc_and_reserve_permissions_stay_stricter_than_ev_permissions() -> None:
    battery = _sample(0.0, "b", 0.0)
    ev = _sample(7000.0, "e", 0.0)
    floor = BatteryControlSession()
    free = BatteryControlSession()
    blocked_floor = _plan(
        floor,
        _settings(exclude_ev=True, soc=12.0, min_soc=12.0),
        now=0.0,
        grid=_sample(8200.0, "g", 0.0),
        battery=battery,
        ev=ev,
    )
    allowed = _plan(
        free,
        _settings(exclude_ev=True, soc=50.0),
        now=0.0,
        grid=_sample(8200.0, "g", 0.0),
        battery=battery,
        ev=ev,
    )
    assert blocked_floor.proposed_w == 0
    assert blocked_floor.reason_code == const.REASON_AT_FLOOR
    assert allowed.proposed_w > 0

    ceiling = BatteryControlSession()
    exporting = BatteryControlSession()
    blocked_ceiling = _plan(
        ceiling,
        _settings(exclude_ev=True, soc=100.0, max_soc=100.0),
        now=0.0,
        grid=_sample(-2500.0, "g", 0.0),
        battery=battery,
        ev=ev,
    )
    charging = _plan(
        exporting,
        _settings(exclude_ev=True, soc=50.0),
        now=0.0,
        grid=_sample(-2500.0, "g", 0.0),
        battery=battery,
        ev=ev,
    )
    assert blocked_ceiling.proposed_w == 0
    assert blocked_ceiling.reason_code == const.REASON_AT_CEILING
    assert charging.proposed_w < 0

    held = BatteryControlSession()
    held_plan = _plan(
        held,
        _settings(
            mode=const.MODE_SELF_CONSUMPTION_RESERVE,
            soc=40.0,
            reserve_target_soc=50.0,
            now=datetime(2026, 4, 27, 15, 0, tzinfo=_TZ),
            exclude_ev=True,
        ),
        now=0.0,
        grid=_sample(8200.0, "g", 0.0),
        battery=battery,
        ev=ev,
    )
    assert held_plan.proposed_w == 0
    assert held_plan.reason_code == const.REASON_RESERVE_HELD
    assert held_plan.release is False


def test_ev_exclusion_never_requests_released() -> None:
    cases = (
        (_sample(7000.0, "g", 0.0), _sample(0.0, "b", 0.0), _sample(7000.0, "e", 0.0)),
        (_sample(8200.0, "g", 0.0), _sample(0.0, "b", 0.0), _sample(7000.0, "e", 0.0)),
        (_sample(-2500.0, "g", 0.0), _sample(0.0, "b", 0.0), _sample(7000.0, "e", 0.0)),
        (_sample(1500.0, "g", 0.0), ChannelSample(value_w=None, available=False), None),
        (_sample(-1800.0, "g", 0.0), ChannelSample(value_w=None, available=False), _sample(-400.0, "e", 0.0)),
        (_sample(1000.0, "g", 0.0), ChannelSample(value_w=None, available=False), _sample(0.0, "e", 0.0)),
        (_sample(2000.0, "g", 0.0), ChannelSample(value_w=None, available=False), _sample(4000.0, "e", 0.0)),
    )
    for grid, battery, ev in cases:
        session = BatteryControlSession()
        session.pd.requested_w = 500
        plan = _plan(
            session,
            _settings(exclude_ev=True),
            now=0.0,
            grid=grid,
            battery=battery,
            ev=ev,
        )
        assert plan.release is False
        assert plan.kind != "released"
        assert plan.reason_code != const.REASON_RELEASED
        assert session.kind != "released"


def test_positive_ev_then_zero_integrates_against_the_consumed_grid() -> None:
    settings = _settings(exclude_ev=True, min_interval_s=1.0)
    excluded = BatteryControlSession()
    followed = BatteryControlSession()
    battery = _sample(0.0, "b", 0.0)
    grid = _sample(8200.0, "g1", 0.0)
    positive = _sample(7000.0, "e-pos", 0.0)
    _plan(excluded, settings, now=0.0, grid=grid, battery=battery, ev=positive)
    _plan(followed, settings, now=0.0, grid=grid, battery=battery, ev=positive)
    assert excluded.publications.last_integrated_grid_publication_id == "g1"
    assert excluded.pd.last_publication_id is not None
    assert excluded.pd.last_publication_id.startswith("ev-known|")
    positive_request = excluded.pd.requested_w

    no_battery = ChannelSample(value_w=None, available=False)
    zero = _plan(
        excluded,
        settings,
        now=1.0,
        grid=_sample(8200.0, "g1", 0.0),
        battery=no_battery,
        ev=_sample(0.0, "e-zero", 1.0),
    )
    raw_grid = _plan(
        followed,
        _settings(exclude_ev=False, min_interval_s=1.0),
        now=1.0,
        grid=_sample(8200.0, "g2", 1.0),
        battery=no_battery,
    )

    assert zero.proposed_w == raw_grid.proposed_w
    assert excluded.pd.requested_w == followed.pd.requested_w
    assert excluded.pd.requested_w != positive_request
    identity = excluded.pd.last_publication_id
    assert identity is not None
    assert identity.startswith("ev-zero|")
    assert "id:g1" in identity
    assert "id:e-zero" in identity
    assert "battery=" not in identity
    assert identity != "g1"
    assert excluded.publications.grid.publication_id == "g1"
    assert excluded.publications.last_integrated_grid_publication_id == "g1"
    assert excluded.pd.last_integrate_mono == 1.0
    assert excluded._feedback_basis == "ev_zero"
    assert zero.release is False


def test_repeated_ev_zero_composite_does_not_integrate_twice() -> None:
    session = BatteryControlSession()
    settings = _settings(exclude_ev=True, min_interval_s=1.0)
    grid = _sample(1000.0, "g1", 0.0)
    ev = _sample(0.0, "e0", 0.0)
    no_battery = ChannelSample(value_w=None, available=False)
    _plan(session, settings, now=0.0, grid=grid, battery=no_battery, ev=ev)
    requested = session.pd.requested_w
    identity = session.pd.last_publication_id
    integrated_at = session.pd.last_integrate_mono

    repeat = _plan(session, settings, now=1.0, grid=grid, battery=no_battery, ev=ev)
    assert repeat.proposed_w == requested
    assert session.pd.requested_w == requested
    assert session.pd.last_publication_id == identity
    assert session.pd.last_integrate_mono == integrated_at
    assert identity is not None
    assert identity.startswith("ev-zero|")
    assert "battery=" not in identity


def test_ev_zero_waits_for_the_interval_gate_then_integrates() -> None:
    session = BatteryControlSession()
    settings = _settings(exclude_ev=True, min_interval_s=5.0)
    _plan(
        session,
        settings,
        now=0.0,
        grid=_sample(8200.0, "g1", 0.0),
        battery=_sample(0.0, "b1", 0.0),
        ev=_sample(7000.0, "e-pos", 0.0),
    )
    held_request = session.pd.requested_w
    held_integrate = session.pd.last_integrate_mono
    no_battery = ChannelSample(value_w=None, available=False)
    ev_zero = _sample(24.0, "e-zero", 1.0)
    held = _plan(
        session,
        settings,
        now=1.0,
        grid=_sample(8200.0, "g1", 0.0),
        battery=no_battery,
        ev=ev_zero,
    )
    assert held.send_w is None
    assert session.pd.requested_w == held_request
    assert session.pd.last_integrate_mono == held_integrate
    assert session.pd.last_publication_id is None
    assert session._feedback_basis == "ev_zero"
    assert session.publications.ev_is_usable()
    assert session.publications.ev.value == 24.0

    opened = _plan(
        session,
        settings,
        now=5.0,
        grid=_sample(8200.0, "g1", 0.0),
        battery=no_battery,
        ev=ev_zero,
    )
    assert opened.proposed_w > held_request
    assert session.pd.requested_w > held_request
    identity = session.pd.last_publication_id
    assert identity is not None
    assert identity.startswith("ev-zero|")
    assert "id:e-zero" in identity
    assert "id:g1" in identity
    assert "battery=" not in identity
    assert session.publications.grid.publication_id == "g1"
    assert session.pd.last_integrate_mono == 1.0


def test_exclude_ev_disabled_uses_the_raw_grid_path() -> None:
    grid = _sample(1000.0, "g-raw", 0.0)
    ev = _sample(0.0, "e0", 0.0)
    disabled = BatteryControlSession()
    raw = BatteryControlSession()
    off = _plan(disabled, _settings(exclude_ev=False), now=0.0, grid=grid, ev=ev)
    reference = _plan(raw, _settings(), now=0.0, grid=grid)
    assert off.proposed_w == reference.proposed_w
    assert disabled.pd.requested_w == raw.pd.requested_w
    assert disabled.pd.last_publication_id == "g-raw"
    assert disabled._feedback_basis == "raw"

    session = BatteryControlSession()
    _plan(
        session,
        _settings(exclude_ev=True, min_interval_s=5.0),
        now=0.0,
        grid=_sample(1000.0, "g0", 0.0),
        ev=_sample(0.0, "e0", 0.0),
    )
    assert session._feedback_basis == "ev_zero"
    session.pd.requested_w = 800
    session.pd.last_direction = 1
    session.pd.grid_filtered = 400.0
    session.pd.zero_cross_holding = True
    session.pd.opposite_since_mono = 0.25
    session.pd.last_opportunity_mono = 0.0
    session.pd.last_integrate_mono = 0.0
    session.ledger.accepted_w = 800
    before = _snapshot(session)
    _plan(
        session,
        _settings(exclude_ev=False, min_interval_s=5.0),
        now=1.0,
        grid=_sample(1000.0, "g0", 0.0),
        ev=_sample(0.0, "e0", 0.0),
        reported=800,
    )
    assert session._feedback_basis == "raw"
    assert session.pd.last_publication_id is None
    assert session.pd.last_source_mono is None
    assert _snapshot(session) == before
    assert session.kind == "pd"
