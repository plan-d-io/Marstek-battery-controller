"""Home Assistant wiring for EV exclusion."""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path

import pytest

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.marstek_battery_controller import _async_update_listener, const
from custom_components.marstek_battery_controller.config_flow import (
    _param_schema_defaults,
    optional_sensors_schema,
)
from custom_components.marstek_battery_controller.coordinator import (
    CoordinatorRuntimeConfig,
    MarstekBatteryCoordinator,
)
from custom_components.marstek_battery_controller.discovery import ResolvedEntities
from custom_components.marstek_battery_controller.ev_input import (
    HA_PROCESS_START_TOLERANCE,
    async_resolve_ha_process_started_utc,
    effective_exclude_ev,
    ev_channel_sample,
    read_ha_process_started_utc,
    requested_exclude_ev,
    resolve_ev_power_entity,
    store_ev_sensor_choice,
)
from custom_components.marstek_battery_controller.storage import MarstekStorage
from custom_components.marstek_battery_controller.switch import MarstekExcludeEvSwitch

_ROOT = Path("custom_components/marstek_battery_controller/translations")
_LANGS = ("en", "en-GB", "nl", "de", "fr")
_GRID = "sensor.grid_power"
_SOC = "sensor.battery_soc"
_BATTERY = "sensor.battery_power"
_EV = "sensor.ev_charge"
_FORCE = "sensor.force_mode"
_CHARGE = "number.charge"
_DISCHARGE = "number.discharge"


def _runtime(ev: str | None = None) -> CoordinatorRuntimeConfig:
    return CoordinatorRuntimeConfig(
        resolved=ResolvedEntities(
            battery_soc=_SOC,
            ac_power=_BATTERY,
            rs485_control="switch.rs485",
            force_mode=_FORCE,
            set_charge_power=_CHARGE,
            set_discharge_power=_DISCHARGE,
        ),
        grid_entity_id=_GRID,
        cap_now_entity_id=None,
        monthly_peak_entity_id=None,
        use_internal_cap_now=True,
        ev_power_entity_id=ev,
    )


class _Writer:
    """Accepts setpoints so a calculation can finish without Modbus."""

    def __init__(self) -> None:
        self.rs485_on = False

    async def async_send_setpoint(self, command_w: int) -> None:
        self.rs485_on = int(command_w) != 0

    async def async_cleanup_released_sequence(self) -> None:
        self.rs485_on = False


async def _coordinator(
    hass,
    ev: str | None = None,
    *,
    process_started=None,
) -> MarstekBatteryCoordinator:
    coordinator = MarstekBatteryCoordinator(
        hass,
        entry_id="entry-test",
        runtime=_runtime(ev),
        storage=MarstekStorage(hass),
        initial_previous_mode=None,
        ha_process_started_utc=process_started or dt_util.utcnow(),
    )
    coordinator.writer = _Writer()
    coordinator._write_grace_until = None
    coordinator._mode = const.MODE_SELF_CONSUMPTION
    coordinator._pd_deadband_w = 0.0
    return coordinator


def _open_gate(coordinator: MarstekBatteryCoordinator) -> None:
    """Allow the next fresh publication to take a normal-sized PD step."""
    coordinator._session.pd.last_opportunity_mono = 0.0
    coordinator._session.pd.last_integrate_mono = 0.0


async def _run(hass, coordinator: MarstekBatteryCoordinator) -> None:
    await coordinator._async_run_calculator()
    await hass.async_block_till_done()


def _publish_core(hass) -> None:
    hass.states.async_set(_GRID, "1000")
    hass.states.async_set(_SOC, "50")
    hass.states.async_set(_BATTERY, "0")
    hass.states.async_set(_FORCE, "stop")
    hass.states.async_set(_CHARGE, "0")
    hass.states.async_set(_DISCHARGE, "0")


def _publish_ev(hass, coordinator, value: str, *, unit: str | None = "W", restored: bool = False, reported: bool = True, when=None) -> None:
    attributes: dict[str, object] = {}
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    if restored:
        attributes["restored"] = True
    hass.states.async_set(_EV, value, attributes)
    state = hass.states.get(_EV)
    assert state is not None
    if reported:
        state.last_reported = when or coordinator.ha_process_started_utc
    else:
        state.last_reported = None


def _schema_keys(schema) -> list[object]:
    return [getattr(key, "schema", key) for key in schema.schema]


def test_ev_entity_resolution_and_requested_defaults() -> None:
    assert resolve_ev_power_entity({}, {}) is None
    assert resolve_ev_power_entity({}, {const.CONF_EV_POWER: "sensor.data"}) == "sensor.data"
    assert resolve_ev_power_entity({const.CONF_EV_POWER: "sensor.opt"}, {const.CONF_EV_POWER: "sensor.data"}) == "sensor.opt"
    assert resolve_ev_power_entity({const.CONF_EV_POWER: None}, {const.CONF_EV_POWER: "sensor.data"}) is None
    assert resolve_ev_power_entity({const.CONF_EV_POWER: "  "}, {const.CONF_EV_POWER: "sensor.data"}) is None

    assert requested_exclude_ev({}, None) is False
    assert effective_exclude_ev(False, None) is False
    assert requested_exclude_ev({}, "sensor.ev") is True
    assert effective_exclude_ev(True, "sensor.ev") is True
    assert requested_exclude_ev({const.CONF_EXCLUDE_EV: False}, "sensor.ev") is False
    assert effective_exclude_ev(False, "sensor.ev") is False
    assert requested_exclude_ev({const.CONF_EXCLUDE_EV: True}, None) is True
    assert effective_exclude_ev(True, None) is False
    assert effective_exclude_ev(True, "sensor.ev") is True


def test_options_flow_can_add_change_and_clear_the_ev_sensor() -> None:
    added: dict[str, object] = {}
    assert store_ev_sensor_choice(added, {const.CONF_EV_POWER: "sensor.ev"}, None) == "sensor.ev"
    assert added[const.CONF_EV_POWER] == "sensor.ev"
    assert const.CONF_EXCLUDE_EV not in added

    changed = {const.CONF_EV_POWER: "sensor.old"}
    assert store_ev_sensor_choice(changed, {const.CONF_EV_POWER: "sensor.new"}, "sensor.old") == "sensor.new"
    assert changed[const.CONF_EV_POWER] == "sensor.new"

    cleared = {const.CONF_EV_POWER: "sensor.old", const.CONF_EXCLUDE_EV: True}
    assert store_ev_sensor_choice(cleared, {}, "sensor.old") is None
    assert cleared[const.CONF_EV_POWER] is None
    assert cleared[const.CONF_EXCLUDE_EV] is True

    default_kept = {const.CONF_EV_POWER: "sensor.old"}
    store_ev_sensor_choice(default_kept, {const.CONF_EV_POWER: ""}, "sensor.old")
    assert default_kept[const.CONF_EV_POWER] is None
    assert default_kept[const.CONF_EXCLUDE_EV] is True

    explicit_false = {const.CONF_EXCLUDE_EV: False, const.CONF_EV_POWER: "sensor.old"}
    store_ev_sensor_choice(explicit_false, {}, "sensor.old")
    assert explicit_false[const.CONF_EXCLUDE_EV] is False
    assert explicit_false[const.CONF_EV_POWER] is None


def test_optional_and_options_schemas_use_a_power_sensor() -> None:
    optional = optional_sensors_schema()
    assert const.CONF_EV_POWER in _schema_keys(optional)
    options = _param_schema_defaults({}, initial=False, include_ev=True, ev_entity="sensor.ev")
    assert const.CONF_EV_POWER in _schema_keys(options)
    marker = next(key for key in options.schema if getattr(key, "schema", None) == const.CONF_EV_POWER)
    config = options.schema[marker].config
    domain = config["domain"]
    domains = domain if isinstance(domain, (list, tuple, set)) else [domain]
    assert "sensor" in domains
    device_class = config["device_class"]
    classes = device_class if isinstance(device_class, (list, tuple, set)) else [device_class]
    assert any(getattr(item, "value", item) == "power" for item in classes)


def _sample(**kwargs):
    start = dt_util.utcnow()
    defaults = dict(
        entity_id=_EV,
        raw_state="1500",
        unit="W",
        restored=False,
        last_reported=start,
        ha_process_started_utc=start,
        now_mono=10.0,
        now_wall=start,
    )
    defaults.update(kwargs)
    return ev_channel_sample(**defaults)


def test_ev_units_and_invalid_values_do_not_use_absolute_power() -> None:
    watts = _sample(raw_state="1500", unit="W")
    kilowatts = _sample(raw_state="1.5", unit="kW")
    assert watts.available and watts.value_w == 1500.0
    assert kilowatts.available and kilowatts.value_w == 1500.0

    for raw, unit in (
        ("-7000", "W"),
        ("nan", "W"),
        ("inf", "W"),
        ("1500", None),
        ("1500", "A"),
        ("1500", "Wh"),
    ):
        sample = _sample(raw_state=raw, unit=unit)
        assert sample.available is False
        assert sample.value_w is None


def test_ev_publication_accepts_only_this_boot_and_keeps_its_identity() -> None:
    start = dt_util.utcnow()
    restored = _sample(restored=True, last_reported=start, ha_process_started_utc=start)
    assert restored.available is False

    within_tolerance = _sample(
        last_reported=start - timedelta(seconds=1),
        ha_process_started_utc=start,
    )
    assert within_tolerance.available is True
    assert HA_PROCESS_START_TOLERANCE == timedelta(seconds=2)

    before = _sample(last_reported=start - timedelta(seconds=30), ha_process_started_utc=start)
    assert before.available is False

    missing = _sample(last_reported=None, ha_process_started_utc=start)
    assert missing.available is False

    accepted = _sample(raw_state="1500", last_reported=start, ha_process_started_utc=start, now_wall=start)
    later = _sample(
        raw_state="1500",
        last_reported=start,
        ha_process_started_utc=start,
        now_mono=50.0,
        now_wall=start + timedelta(days=2),
    )
    assert accepted.available and later.available
    assert accepted.value_w == later.value_w == 1500.0
    assert accepted.publication_id == later.publication_id
    assert later.wall_age_s is not None and later.wall_age_s > 100000


def test_translation_parity_for_ev_exclusion() -> None:
    paths = (
        ("config", "step", "optional", "description"),
        ("config", "step", "optional", "data", "ev_power_entity"),
        ("config", "step", "init", "description"),
        ("config", "step", "init", "data", "ev_power_entity"),
        ("entity", "switch", "exclude_ev", "name"),
    )
    for language in _LANGS:
        payload = json.loads((_ROOT / f"{language}.json").read_text(encoding="utf-8"))
        for path in paths:
            cursor = payload
            for key in path:
                assert key in cursor, f"{language} missing {'.'.join(path)}"
                cursor = cursor[key]
            assert isinstance(cursor, str) and cursor.strip()
    english = json.loads((_ROOT / "en.json").read_text(encoding="utf-8"))
    assert english["entity"]["switch"]["exclude_ev"]["name"] == "Exclude EV consumption"


async def test_existing_entry_without_ev_stays_on_raw_grid_and_hides_the_switch(hass) -> None:
    coordinator = await _coordinator(hass, ev=None)
    coordinator.configure_startup_ev(None, {})
    assert coordinator.exclude_ev_requested is False
    assert coordinator.exclude_ev_effective is False
    assert const.CONF_EXCLUDE_EV not in coordinator.as_options_dict()
    _publish_core(hass)
    hass.states.async_set(_GRID, "7000")
    await _run(hass, coordinator)
    assert coordinator.target_setpoint > 0
    assert coordinator._mode == const.MODE_SELF_CONSUMPTION

    coordinator.last_update_success = True
    switch = MarstekExcludeEvSwitch(coordinator, "entry-test", DeviceInfo())
    assert switch.available is False
    assert switch.is_on is False
    assert switch.translation_key == const.ENTITY_EXCLUDE_EV


async def test_first_sensor_defaults_exclusion_on_before_the_first_calculation(hass) -> None:
    raw = await _coordinator(hass, ev=None)
    raw.configure_startup_ev(None, {})
    excluded = await _coordinator(hass, ev=_EV)
    excluded.configure_startup_ev(_EV, {})
    assert excluded.exclude_ev_requested is True
    assert excluded.exclude_ev_effective is True
    _publish_core(hass)
    hass.states.async_set(_GRID, "7000")
    hass.states.async_set(_BATTERY, "0")
    _publish_ev(hass, excluded, "7000")
    await _run(hass, raw)
    await _run(hass, excluded)
    assert raw.target_setpoint > 0
    assert excluded.target_setpoint == 0
    assert excluded._mode != const.MODE_RELEASED


async def test_explicit_false_is_honored_and_removed_sensor_keeps_the_request(hass) -> None:
    coordinator = await _coordinator(hass, ev=_EV)
    coordinator.configure_startup_ev(_EV, {const.CONF_EXCLUDE_EV: False})
    assert coordinator.exclude_ev_requested is False
    assert coordinator.exclude_ev_effective is False

    stored = await _coordinator(hass, ev=None)
    stored.configure_startup_ev(None, {const.CONF_EXCLUDE_EV: True, const.CONF_EV_POWER: None})
    assert stored.exclude_ev_requested is True
    assert stored.exclude_ev_effective is False
    stored.last_update_success = True
    assert MarstekExcludeEvSwitch(stored, "entry-test", DeviceInfo()).available is False

    restored = await _coordinator(hass, ev=_EV)
    restored.configure_startup_ev(_EV, {const.CONF_EXCLUDE_EV: True, const.CONF_EV_POWER: _EV})
    assert restored.exclude_ev_requested is True
    assert restored.exclude_ev_effective is True


async def test_coordinator_rejects_bad_ev_samples_and_uses_valid_ones(hass) -> None:
    coordinator = await _coordinator(hass, ev=_EV)
    coordinator.configure_startup_ev(_EV, {})
    _publish_core(hass)
    hass.states.async_set(_GRID, "2000")
    hass.states.async_set(_BATTERY, "0")

    _publish_ev(hass, coordinator, "-1500")
    await _run(hass, coordinator)
    assert coordinator._session.publications.ev.available is False
    assert coordinator.target_setpoint == 0

    _publish_ev(hass, coordinator, "1500", unit=None)
    await _run(hass, coordinator)
    assert coordinator._session.publications.ev.available is False

    hass.states.async_set(_GRID, "1500")
    _publish_ev(hass, coordinator, "1.5", unit="kW", when=coordinator.ha_process_started_utc + timedelta(seconds=1))
    await _run(hass, coordinator)
    assert coordinator._session.publications.ev.value == 1500.0
    assert coordinator.target_setpoint == 0


async def test_restored_and_pre_boot_ev_states_are_ignored(hass) -> None:
    coordinator = await _coordinator(hass, ev=_EV)
    coordinator.configure_startup_ev(_EV, {})
    _publish_core(hass)
    hass.states.async_set(_GRID, "8200")
    hass.states.async_set(_BATTERY, "0")

    _publish_ev(hass, coordinator, "7000", restored=True)
    await _run(hass, coordinator)
    assert coordinator._session.publications.ev.available is False
    assert coordinator.target_setpoint == 0

    _publish_ev(
        hass,
        coordinator,
        "7000",
        when=coordinator.ha_process_started_utc - timedelta(minutes=1),
    )
    await _run(hass, coordinator)
    assert coordinator._session.publications.ev.available is False

    _publish_ev(hass, coordinator, "7000", reported=False)
    await _run(hass, coordinator)
    assert coordinator._session.publications.ev.available is False
    assert coordinator._mode == const.MODE_SELF_CONSUMPTION


async def test_accepted_ev_stays_usable_until_it_is_dropped_and_can_recover(hass) -> None:
    coordinator = await _coordinator(hass, ev=_EV)
    coordinator.configure_startup_ev(_EV, {})
    _publish_core(hass)
    hass.states.async_set(_GRID, "8200")
    hass.states.async_set(_BATTERY, "0")
    _publish_ev(hass, coordinator, "7000")
    await _run(hass, coordinator)
    identity = coordinator._session.publications.ev.publication_id
    assert coordinator._session.publications.ev_is_usable()
    assert coordinator.target_setpoint > 0

    await _run(hass, coordinator)
    assert coordinator._session.publications.ev_is_usable()
    assert coordinator._session.publications.ev.value == 7000.0
    assert coordinator._session.publications.ev.publication_id == identity

    _publish_ev(hass, coordinator, "unavailable", unit=None)
    await _run(hass, coordinator)
    assert coordinator._session.publications.ev.available is False
    assert coordinator.target_setpoint == 0
    assert coordinator.exclude_ev_effective is True
    assert coordinator._mode == const.MODE_SELF_CONSUMPTION
    assert "ev" not in coordinator._sensor_bad_since

    _publish_ev(hass, coordinator, "7000", when=dt_util.utcnow())
    _open_gate(coordinator)
    await _run(hass, coordinator)
    assert coordinator._session.publications.ev_is_usable()
    assert coordinator.target_setpoint > 0
    assert coordinator._mode != const.MODE_RELEASED


async def test_ev_state_changes_recalculate_without_becoming_a_critical_sensor(hass) -> None:
    coordinator = await _coordinator(hass, ev=_EV)
    coordinator.configure_startup_ev(_EV, {})
    _publish_core(hass)
    _publish_ev(hass, coordinator, "0")

    class _Writer:
        rs485_on = False

        async def async_cleanup_released_sequence(self) -> None:
            return None

        async def async_send_setpoint(self, command_w: int) -> None:
            return None

    coordinator.writer = _Writer()
    calls = {"n": 0}
    original = coordinator._async_run_calculator

    async def _wrapped(*args, **kwargs):
        calls["n"] += 1
        return await original(*args, **kwargs)

    coordinator._async_run_calculator = _wrapped  # type: ignore[method-assign]
    coordinator.async_setup_listeners()
    await hass.async_block_till_done()
    baseline = calls["n"]
    assert baseline >= 1

    _publish_ev(hass, coordinator, "500", when=coordinator.ha_process_started_utc + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert calls["n"] > baseline
    assert "ev" not in coordinator._sensor_bad_since
    await coordinator.async_shutdown()


async def test_raw_grid_still_feeds_capacity_and_diagnostics(hass) -> None:
    coordinator = await _coordinator(hass, ev=_EV)
    coordinator.configure_startup_ev(_EV, {})
    _publish_core(hass)
    hass.states.async_set(_GRID, "1000")
    _publish_ev(hass, coordinator, "4000")
    await _run(hass, coordinator)
    assert coordinator.snapshot_grid_smoothed == 1000
    assert coordinator.snapshot_cap_now == 1000


async def test_switch_persists_the_requested_choice_and_recalculates(hass) -> None:
    entry = MockConfigEntry(domain=const.DOMAIN, data={const.CONF_EV_POWER: _EV}, options={const.CONF_EXCLUDE_EV: False}, entry_id="entry-test")
    entry.add_to_hass(hass)
    coordinator = await _coordinator(hass, ev=_EV)
    coordinator.configure_startup_ev(_EV, {const.CONF_EXCLUDE_EV: False})
    hass.data.setdefault(const.DOMAIN, {})[entry.entry_id] = coordinator
    _publish_core(hass)
    hass.states.async_set(_GRID, "5000")
    hass.states.async_set(_BATTERY, "0")
    _publish_ev(hass, coordinator, "1000")
    await _run(hass, coordinator)
    assert coordinator.target_setpoint > 0

    switch = MarstekExcludeEvSwitch(coordinator, entry.entry_id, DeviceInfo())
    switch.hass = hass
    switch.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    coordinator.last_update_success = True
    assert switch.available is True
    assert switch.is_on is False

    _publish_ev(hass, coordinator, "unavailable", unit=None)
    assert switch.available is True

    await switch.async_turn_on()
    await hass.async_block_till_done()
    assert switch.is_on is True
    assert entry.options[const.CONF_EXCLUDE_EV] is True
    assert coordinator.exclude_ev_effective is True
    assert coordinator.target_setpoint == 0
    assert coordinator._mode == const.MODE_SELF_CONSUMPTION

    hass.states.async_set(_GRID, "5100")
    _open_gate(coordinator)
    await switch.async_turn_off()
    await hass.async_block_till_done()
    assert switch.is_on is False
    assert entry.options[const.CONF_EXCLUDE_EV] is False
    assert coordinator.exclude_ev_effective is False
    assert coordinator.target_setpoint > 0


async def test_listener_reloads_for_an_ev_entity_change_only(hass) -> None:
    coordinator = await _coordinator(hass, ev=_EV)
    coordinator.configure_startup_ev(_EV, {const.CONF_EXCLUDE_EV: True})
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={const.CONF_EV_POWER: _EV},
        options={const.CONF_EV_POWER: _EV, const.CONF_EXCLUDE_EV: True},
        entry_id="entry-test",
    )
    entry.add_to_hass(hass)
    hass.data.setdefault(const.DOMAIN, {})[entry.entry_id] = coordinator
    scheduled: list[str] = []

    def _schedule(entry_id: str) -> None:
        scheduled.append(entry_id)

    hass.config_entries.async_schedule_reload = _schedule  # type: ignore[method-assign]
    hass.config_entries.async_update_entry(entry, options={const.CONF_EV_POWER: _EV, const.CONF_EXCLUDE_EV: False})
    await _async_update_listener(hass, entry)
    assert scheduled == []
    assert coordinator.exclude_ev_requested is False
    assert coordinator.runtime.ev_power_entity_id == _EV

    hass.config_entries.async_update_entry(entry, options={const.CONF_EV_POWER: "sensor.other", const.CONF_EXCLUDE_EV: False})
    await _async_update_listener(hass, entry)
    assert scheduled == [entry.entry_id]

    scheduled.clear()
    hass.config_entries.async_update_entry(
        entry,
        options={const.CONF_EV_POWER: None, const.CONF_EXCLUDE_EV: True},
    )
    coordinator.runtime.ev_power_entity_id = _EV
    await _async_update_listener(hass, entry)
    assert scheduled == [entry.entry_id]


async def test_report_before_coordinator_creation_survives_reload(hass) -> None:
    process_started = dt_util.utcnow() - timedelta(minutes=10)
    reported = process_started + timedelta(minutes=1)
    coordinator = await _coordinator(hass, ev=_EV, process_started=process_started)
    coordinator.configure_startup_ev(_EV, {})
    _publish_core(hass)
    hass.states.async_set(_GRID, "8200")
    hass.states.async_set(_BATTERY, "0")
    _publish_ev(hass, coordinator, "7000", when=reported)
    await _run(hass, coordinator)
    assert reported < dt_util.utcnow()
    assert coordinator._session.publications.ev_is_usable()
    assert coordinator._session.publications.ev.value == 7000.0
    assert coordinator.target_setpoint > 0
    assert coordinator._mode != const.MODE_RELEASED

    reloaded = await _coordinator(hass, ev=_EV, process_started=process_started)
    reloaded.configure_startup_ev(_EV, {})
    await _run(hass, reloaded)
    assert reloaded.ha_process_started_utc == process_started
    assert reloaded._session.publications.ev_is_usable()
    assert reloaded._session.publications.ev.value == 7000.0
    assert reloaded._mode != const.MODE_RELEASED


async def test_process_start_lookup_failure_uses_setup_time(hass, monkeypatch, caplog) -> None:
    def _fail() -> None:
        raise OSError("psutil unavailable")

    monkeypatch.setattr(
        "custom_components.marstek_battery_controller.ev_input.read_ha_process_started_utc",
        _fail,
    )
    caplog.set_level("WARNING")
    before = dt_util.utcnow()
    started = await async_resolve_ha_process_started_utc(hass)
    after = dt_util.utcnow()
    assert before <= started <= after
    assert "process start" in caplog.text.lower()

    coordinator = await _coordinator(hass, ev=_EV, process_started=started)
    coordinator.configure_startup_ev(_EV, {})
    _publish_core(hass)
    hass.states.async_set(_GRID, "2000")
    _publish_ev(hass, coordinator, "1500", when=started - timedelta(minutes=1))
    await _run(hass, coordinator)
    assert coordinator._session.publications.ev.available is False
    assert coordinator._mode == const.MODE_SELF_CONSUMPTION


async def test_process_start_is_read_in_an_executor_and_stable_across_reload(hass, monkeypatch) -> None:
    expected = dt_util.utcnow() - timedelta(hours=1)
    seen: list[object] = []
    real = hass.async_add_executor_job

    def _spy(func, *args):
        seen.append(func)
        return real(func, *args)

    monkeypatch.setattr(hass, "async_add_executor_job", _spy)
    monkeypatch.setattr(
        "custom_components.marstek_battery_controller.ev_input.read_ha_process_started_utc",
        lambda: expected,
    )
    first = await async_resolve_ha_process_started_utc(hass)
    second = await async_resolve_ha_process_started_utc(hass)
    assert first == second == expected
    assert seen


def test_installed_psutil_reports_an_aware_process_start() -> None:
    pytest.importorskip("psutil_home_assistant")
    started = read_ha_process_started_utc()
    assert started.tzinfo is not None
    assert started <= dt_util.utcnow() + timedelta(seconds=2)


async def test_same_value_ev_report_recalculates_once_and_unsubscribes(hass, monkeypatch) -> None:
    from homeassistant.helpers import event as ha_event

    real = getattr(ha_event, "async_track_state_report_event", None)
    live: list[dict[str, object]] = []

    def _track(hass, entity_ids, action, *args, **kwargs):
        ids = [entity_ids] if isinstance(entity_ids, str) else list(entity_ids)
        assert ids == [_EV]
        entry = {"action": action, "alive": True}
        live.append(entry)
        real_unsub = real(hass, entity_ids, action, *args, **kwargs) if real is not None else None

        def _unsub() -> None:
            entry["alive"] = False
            if real_unsub is not None:
                real_unsub()

        return _unsub

    monkeypatch.setattr(ha_event, "async_track_state_report_event", _track, raising=False)

    def _count(coordinator: MarstekBatteryCoordinator) -> dict[str, int]:
        calls = {"n": 0}
        original = coordinator._async_run_calculator

        async def _wrapped(*args, **kwargs):
            calls["n"] += 1
            return await original(*args, **kwargs)

        coordinator._async_run_calculator = _wrapped  # type: ignore[method-assign]
        return calls

    process_started = dt_util.utcnow() - timedelta(minutes=5)
    first = await _coordinator(hass, ev=_EV, process_started=process_started)
    first.configure_startup_ev(_EV, {})
    _publish_core(hass)
    hass.states.async_set(_GRID, "8200")
    hass.states.async_set(_BATTERY, "0")
    _publish_ev(hass, first, "7000", when=process_started + timedelta(minutes=1))
    first_calls = _count(first)
    first.async_setup_listeners()
    await hass.async_block_till_done()
    baseline = first_calls["n"]
    assert baseline >= 1
    assert len(live) == 1

    async def _report_same_value() -> None:
        if real is None:
            state = hass.states.get(_EV)
            assert state is not None
            state.last_reported = dt_util.utcnow()
            for entry in live:
                if entry["alive"]:
                    entry["action"](None)  # type: ignore[operator]
            return
        hass.states.async_set(_EV, "7000", {"unit_of_measurement": "W"})

    await _report_same_value()
    await hass.async_block_till_done()
    assert first_calls["n"] == baseline + 1

    await first.async_shutdown()
    assert live[0]["alive"] is False
    stalled = first_calls["n"]
    await _report_same_value()
    await hass.async_block_till_done()
    assert first_calls["n"] == stalled

    second = await _coordinator(hass, ev=_EV, process_started=process_started)
    second.configure_startup_ev(_EV, {})
    second_calls = _count(second)
    second.async_setup_listeners()
    await hass.async_block_till_done()
    second_baseline = second_calls["n"]
    await _report_same_value()
    await hass.async_block_till_done()
    assert second_calls["n"] == second_baseline + 1
    assert first_calls["n"] == stalled
    await second.async_shutdown()
