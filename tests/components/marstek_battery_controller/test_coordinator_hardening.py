"""Home Assistant tests for coordinator restart, dispatch, and release safety."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from custom_components.marstek_battery_controller import const
from custom_components.marstek_battery_controller.coordinator import (
    CoordinatorRuntimeConfig,
    MarstekBatteryCoordinator,
)
from custom_components.marstek_battery_controller.discovery import ResolvedEntities
from custom_components.marstek_battery_controller.pd_session import ControlPlan
from custom_components.marstek_battery_controller.select import MarstekModeSelect
from custom_components.marstek_battery_controller.storage import MarstekStorage

pytest.importorskip("homeassistant")

from homeassistant.util import dt as dt_util

GRID = "sensor.grid_power"
SOC = "sensor.battery_soc"
BATTERY = "sensor.battery_ac_power"
FORCE = "select.force_mode"
CHARGE = "number.set_charge_power"
DISCHARGE = "number.set_discharge_power"


class _FakeWriter:
    """Records Modbus writes without calling Home Assistant services."""

    def __init__(self) -> None:
        self.sends: list[int] = []
        self.cleanups = 0
        self.rs485_on = False
        self.active = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def async_send_setpoint(self, setpoint_w: int) -> None:
        self.sends.append(int(setpoint_w))
        self.rs485_on = int(setpoint_w) != 0
        self.started.set()
        if self.block:
            self.active += 1
            try:
                await self.release.wait()
            finally:
                self.active -= 1

    async def async_cleanup_released_sequence(self) -> None:
        self.cleanups += 1
        self.rs485_on = False


def _runtime() -> CoordinatorRuntimeConfig:
    return CoordinatorRuntimeConfig(
        resolved=ResolvedEntities(
            battery_soc=SOC,
            ac_power=BATTERY,
            rs485_control="switch.rs485",
            force_mode=FORCE,
            set_charge_power=CHARGE,
            set_discharge_power=DISCHARGE,
        ),
        grid_entity_id=GRID,
        cap_now_entity_id=None,
        monthly_peak_entity_id=None,
        use_internal_cap_now=True,
    )


def _publish(hass, *, soc: str = "50", grid: str = "1000", battery: str = "0",
             force: str = "stop", charge: str | None = "0", discharge: str | None = "0") -> None:
    hass.states.async_set(GRID, grid)
    hass.states.async_set(SOC, soc)
    hass.states.async_set(BATTERY, battery)
    hass.states.async_set(FORCE, force)
    if charge is None:
        hass.states.async_remove(CHARGE)
    else:
        hass.states.async_set(CHARGE, charge)
    if discharge is None:
        hass.states.async_remove(DISCHARGE)
    else:
        hass.states.async_set(DISCHARGE, discharge)


async def _coordinator(hass) -> tuple[MarstekBatteryCoordinator, _FakeWriter]:
    coordinator = MarstekBatteryCoordinator(
        hass,
        entry_id="entry-test",
        runtime=_runtime(),
        storage=MarstekStorage(hass),
        initial_previous_mode=None,
    )
    writer = _FakeWriter()
    coordinator.writer = writer
    return coordinator, writer


def _command(command_w: int) -> ControlPlan:
    return ControlPlan(
        kind="pd",
        proposed_w=command_w,
        send_w=command_w,
        safety=False,
        reassert=False,
        release=False,
        operating_state=const.STATE_SELF_CONSUMPTION,
        reason_code=const.REASON_NORMAL,
    )


def _allow_writes(coordinator: MarstekBatteryCoordinator) -> None:
    coordinator._write_grace_until = None


async def test_first_refresh_cannot_write_during_restart_grace(hass) -> None:
    """Grace exists before the first refresh, so setup cannot dispatch."""
    coordinator, writer = await _coordinator(hass)
    assert coordinator._write_grace_until is not None
    assert coordinator._write_grace_until > dt_util.utcnow()
    _publish(hass)
    coordinator.set_pd_grid_deadband(0)
    coordinator.set_mode(const.MODE_SELF_CONSUMPTION)
    await coordinator.async_config_entry_first_refresh()
    await hass.async_block_till_done()
    assert writer.sends == []
    assert writer.cleanups == 0
    assert coordinator._write_grace_until is not None


async def test_simultaneous_dispatch_does_not_deadlock_or_reorder(hass) -> None:
    """One in-flight write stays first. A newer command does not jump the queue."""
    coordinator, writer = await _coordinator(hass)
    _allow_writes(coordinator)
    writer.block = True
    coordinator._dispatch_plan(_command(100))
    await asyncio.wait_for(writer.started.wait(), timeout=2)
    coordinator._dispatch_plan(_command(200))
    coordinator._dispatch_plan(_command(300))
    assert coordinator._active_dispatch is not None
    assert coordinator._active_dispatch.command_w == 100
    assert coordinator._queued_dispatch is not None
    assert coordinator._queued_dispatch.command_w == 300
    writer.release.set()
    assert coordinator._command_task is not None
    await asyncio.wait_for(coordinator._command_task, timeout=2)
    await hass.async_block_till_done()
    assert writer.sends == [100, 300]
    assert coordinator._session.ledger.pending_w is None
    assert coordinator._active_dispatch is None


async def test_matching_inflight_command_discards_older_pending(hass) -> None:
    """The command already in flight replaces any older different pending command."""
    coordinator, writer = await _coordinator(hass)
    _allow_writes(coordinator)
    writer.block = True
    coordinator._dispatch_plan(_command(100))
    await asyncio.wait_for(writer.started.wait(), timeout=2)
    coordinator._dispatch_plan(_command(200))
    assert coordinator._queued_dispatch is not None
    coordinator._dispatch_plan(_command(100))
    assert coordinator._queued_dispatch is None
    writer.release.set()
    assert coordinator._command_task is not None
    await asyncio.wait_for(coordinator._command_task, timeout=2)
    assert writer.sends == [100]
    assert coordinator._session.ledger.accepted_w == 100


async def test_concurrent_recalculation_finishes(hass) -> None:
    """Two calculator runs can overlap a write without hanging or reversing it."""
    coordinator, writer = await _coordinator(hass)
    _allow_writes(coordinator)
    _publish(hass, grid="1000", force="stop")
    coordinator.set_pd_grid_deadband(0)
    coordinator.set_mode(const.MODE_SELF_CONSUMPTION)
    writer.block = True
    first = asyncio.create_task(coordinator._async_run_calculator())
    await asyncio.wait_for(writer.started.wait(), timeout=2)
    second = asyncio.create_task(coordinator._async_run_calculator())
    await asyncio.wait_for(second, timeout=2)
    writer.release.set()
    await asyncio.wait_for(first, timeout=2)
    await hass.async_block_till_done()
    assert writer.sends
    assert writer.sends == sorted(writer.sends, key=lambda value: writer.sends.index(value))
    assert coordinator._command_task is None or coordinator._command_task.done()


async def test_every_released_transition_cleans_up_once(hass) -> None:
    """Each way into Released schedules one cleanup, and a repeat does not."""
    from custom_components.marstek_battery_controller import _apply_options

    async def opened() -> tuple[MarstekBatteryCoordinator, _FakeWriter]:
        coordinator, writer = await _coordinator(hass)
        _allow_writes(coordinator)
        _publish(hass)
        return coordinator, writer

    startup, startup_writer = await opened()
    startup.ensure_released_cleanup()
    await hass.async_block_till_done()
    startup.ensure_released_cleanup()
    await hass.async_block_till_done()
    assert startup.current_mode == const.MODE_RELEASED
    assert startup_writer.cleanups == 1
    assert startup_writer.rs485_on is False

    options, options_writer = await opened()
    options.set_mode(const.MODE_SELF_CONSUMPTION)
    await hass.async_block_till_done()
    options_writer.cleanups = 0
    _apply_options(options, {"mode": const.MODE_RELEASED})
    await hass.async_block_till_done()
    _apply_options(options, {"mode": const.MODE_RELEASED})
    await hass.async_block_till_done()
    assert options_writer.cleanups == 1

    ui, ui_writer = await opened()
    ui.set_mode(const.MODE_SELF_CONSUMPTION)
    await hass.async_block_till_done()
    ui_writer.cleanups = 0
    select = MarstekModeSelect(ui, "entry-test", {"identifiers": {("test", "battery")}})
    select.hass = hass
    select.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    await select.async_select_option(const.MODE_RELEASED)
    await hass.async_block_till_done()
    await select.async_select_option(const.MODE_RELEASED)
    await hass.async_block_till_done()
    assert ui_writer.cleanups == 1

    manual, manual_writer = await opened()
    manual.set_mode(const.MODE_SELF_CONSUMPTION)
    await hass.async_block_till_done()
    manual.previous_mode = const.MODE_RELEASED
    manual._mode = const.MODE_MANUAL
    manual_writer.cleanups = 0
    await manual._async_check_manual_auto_exit()
    await hass.async_block_till_done()
    await manual._async_check_manual_auto_exit()
    await hass.async_block_till_done()
    assert manual.current_mode == const.MODE_RELEASED
    assert manual_writer.cleanups == 1

    timeout, timeout_writer = await opened()
    timeout.set_mode(const.MODE_SELF_CONSUMPTION)
    await hass.async_block_till_done()
    timeout_writer.cleanups = 0
    timeout._session.ledger.accepted_w = 400
    _publish(hass, soc="unavailable", force="unavailable", charge=None, discharge=None)
    timeout._sensor_bad_since["soc"] = asyncio.get_running_loop().time() - (
        const.SENSOR_UNAVAILABLE_RELEASE_S + 1
    )
    await timeout._async_run_calculator()
    await hass.async_block_till_done()
    assert timeout.current_mode == const.MODE_RELEASED
    assert timeout_writer.cleanups == 1
    await timeout._async_run_calculator()
    await hass.async_block_till_done()
    assert timeout_writer.cleanups == 1


async def test_readbacks_reconcile_only_when_fresh_and_newer_than_the_write(hass, freezer) -> None:
    """Stale or incoherent telemetry cannot replace an accepted command."""
    freezer.move_to("2026-04-27 12:00:00+00:00")
    coordinator, _writer = await _coordinator(hass)
    _allow_writes(coordinator)
    coordinator.set_mode(const.MODE_SELF_CONSUMPTION)
    coordinator._session.kind = "pd"
    _publish(hass, force="discharge", discharge="400", charge="0")
    adopted = coordinator._reported_observation(0.0, dt_util.utcnow())
    assert adopted.command_w == 400
    assert adopted.coherent is True
    assert adopted.fresh is True
    assert coordinator._session.consider_reported(adopted, now_mono=0.0) is True
    assert coordinator._session.ledger.accepted_w == 400
    assert coordinator._session.pd.last_direction == 1

    coordinator._session.ledger.last_success_mono = 50.0
    freezer.tick(timedelta(seconds=1))
    _publish(hass, force="charge", charge="250", discharge="0")
    stale_after_write = coordinator._reported_observation(10.0, dt_util.utcnow())
    assert coordinator._session.consider_reported(stale_after_write, now_mono=10.0) is False
    assert coordinator._session.ledger.accepted_w == 400
    assert coordinator._session.pd.last_direction == 1

    _publish(hass, force="charge", charge=None, discharge="0")
    missing_charge = coordinator._reported_observation(10.0, dt_util.utcnow())
    assert missing_charge.command_w is None
    assert missing_charge.coherent is False
    assert coordinator._session.consider_reported(missing_charge, now_mono=10.0) is False

    _publish(hass, force="discharge", discharge=None, charge="0")
    missing_discharge = coordinator._reported_observation(10.0, dt_util.utcnow())
    assert missing_discharge.command_w is None
    assert missing_discharge.coherent is False

    freezer.tick(timedelta(seconds=11))
    aged = coordinator._reported_observation(30.0, dt_util.utcnow())
    assert aged.fresh is False
    assert coordinator._session.consider_reported(aged, now_mono=30.0) is False
    assert coordinator._session.ledger.accepted_w == 400

    coordinator._session.ledger.last_success_mono = 0.0
    coordinator._write_grace_until = dt_util.utcnow() + timedelta(seconds=60)
    _publish(hass, force="charge", charge="250", discharge="0")
    external = coordinator._reported_observation(40.0, dt_util.utcnow())
    assert external.fresh is True
    assert external.command_w == -250
    await coordinator._async_run_calculator()
    assert coordinator._session.ledger.accepted_w == -250
    assert coordinator._session.pd.last_direction == -1


async def test_stale_soc_sends_safety_zero_then_released(hass, freezer, monkeypatch) -> None:
    """A stale SoC zeros battery power and the existing timeout then releases it."""
    freezer.move_to("2026-04-27 12:00:00+00:00")
    coordinator, writer = await _coordinator(hass)
    _allow_writes(coordinator)
    _publish(hass, soc="50", grid="1000")
    coordinator.set_pd_grid_deadband(0)
    coordinator.set_mode(const.MODE_SELF_CONSUMPTION)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=31))
    hass.states.async_set(GRID, "1000")
    hass.states.async_set(BATTERY, "0")
    hass.states.async_set(FORCE, "discharge")
    hass.states.async_set(DISCHARGE, "400")
    coordinator._session.pd.requested_w = 400
    coordinator._session.pd.last_integrate_mono = 1.0
    coordinator._session.ledger.accepted_w = 400
    monkeypatch.setattr(const, "SENSOR_UNAVAILABLE_RELEASE_S", 0)
    writer.sends.clear()
    await coordinator._async_run_calculator()
    await hass.async_block_till_done()
    assert coordinator.current_mode == const.MODE_SELF_CONSUMPTION
    assert 0 in writer.sends
    assert coordinator._session.pd.requested_w == 400
    assert coordinator._session.pd.last_integrate_mono == 1.0
    await coordinator._async_run_calculator()
    await hass.async_block_till_done()
    assert coordinator.current_mode == const.MODE_RELEASED
    assert writer.cleanups == 1


async def test_unload_leaves_no_command_or_cleanup_task(hass) -> None:
    """Unload cancels the write, then cleanup, and returns with nothing running."""
    coordinator, writer = await _coordinator(hass)
    _allow_writes(coordinator)
    coordinator.set_mode(const.MODE_SELF_CONSUMPTION)
    writer.block = True
    writer.rs485_on = True
    coordinator._dispatch_plan(_command(500))
    await asyncio.wait_for(writer.started.wait(), timeout=2)
    seen: dict[str, bool] = {}
    original = coordinator._session.ledger.note_cancelled

    def note_cancelled(command_w: int, *, call_started: bool) -> None:
        seen["call_started"] = call_started
        original(command_w, call_started=call_started)

    coordinator._session.ledger.note_cancelled = note_cancelled  # type: ignore[method-assign]
    await coordinator.async_shutdown()
    assert writer.active == 0
    assert coordinator._command_task is None
    assert coordinator._active_dispatch is None
    assert coordinator._queued_dispatch is None
    assert coordinator._released_cleanup_task is None
    assert coordinator._released_cleanup_unsub is None
    assert writer.cleanups == 1
    assert writer.rs485_on is False
    assert coordinator._session.ledger.in_flight_w is None
    assert coordinator._session.ledger.accepted_w != 500
    assert seen["call_started"] is True
    running = [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task is not asyncio.current_task()
    ]
    assert not any("released" in str(task.get_coro()) or "write_command" in str(task.get_coro()) for task in running)


async def test_active_mode_waits_for_physical_released_cleanup(hass) -> None:
    """A running cleanup stays exclusive, then the new mode is calculated once."""
    coordinator, writer = await _coordinator(hass)
    _allow_writes(coordinator)
    _publish(hass, grid="1000", soc="50", battery="0", force="stop")
    coordinator.set_pd_grid_deadband(0)
    gate = asyncio.Event()
    order: list[str] = []

    async def cleanup() -> None:
        order.append("cleanup")
        writer.cleanups += 1
        writer.started.set()
        await gate.wait()
        order.append("cleanup-end")
        writer.rs485_on = False

    async def send(setpoint_w: int) -> None:
        order.append("send")
        writer.sends.append(int(setpoint_w))
        writer.rs485_on = int(setpoint_w) != 0

    writer.async_cleanup_released_sequence = cleanup  # type: ignore[method-assign]
    writer.async_send_setpoint = send  # type: ignore[method-assign]
    coordinator.ensure_released_cleanup()
    await asyncio.wait_for(writer.started.wait(), timeout=2)
    assert coordinator._cleanup_physical is True
    coordinator.set_mode(const.MODE_SELF_CONSUMPTION)
    assert writer.sends == []
    assert coordinator._mode == const.MODE_RELEASED
    gate.set()
    await hass.async_block_till_done()
    assert order.index("cleanup-end") < order.index("send")
    assert writer.cleanups == 1
    assert coordinator._mode == const.MODE_SELF_CONSUMPTION
    assert writer.rs485_on is True


async def test_deferred_cleanup_timer_can_be_cancelled(hass) -> None:
    """A cleanup that has not touched Modbus is dropped when the mode leaves Released."""
    coordinator, writer = await _coordinator(hass)
    _publish(hass)
    coordinator.ensure_released_cleanup()
    await hass.async_block_till_done()
    assert coordinator._released_cleanup_unsub is not None
    assert writer.cleanups == 0
    coordinator.set_mode(const.MODE_SELF_CONSUMPTION)
    await hass.async_block_till_done()
    assert coordinator._released_cleanup_unsub is None
    assert writer.cleanups == 0
    _allow_writes(coordinator)
    coordinator.set_pd_grid_deadband(0)
    await coordinator._async_run_calculator()
    await hass.async_block_till_done()
    assert writer.sends
    assert writer.cleanups == 0


async def test_unload_cleans_scheduled_and_inherited_released(hass) -> None:
    """A timer or a Released startup still owes one bounded cleanup on unload."""
    coordinator, writer = await _coordinator(hass)
    coordinator.ensure_released_cleanup()
    await hass.async_block_till_done()
    assert coordinator._released_cleanup_unsub is not None
    assert writer.rs485_on is False
    assert coordinator._session.ledger.accepted_w == 0
    assert coordinator._command_task is None
    assert coordinator._mode == const.MODE_RELEASED
    await coordinator.async_shutdown()
    assert writer.cleanups == 1
    assert coordinator._released_cleanup_unsub is None
    assert coordinator._released_cleanup_task is None

    inherited, inherited_writer = await _coordinator(hass)
    assert inherited._mode == const.MODE_RELEASED
    assert inherited._inherited_released_uncertainty is True
    assert inherited._release_cleanup_owned is False
    await inherited.async_shutdown()
    assert inherited_writer.cleanups == 1
    assert inherited._released_cleanup_task is None


async def test_failed_released_cleanup_retries_once(hass) -> None:
    """One failed cleanup is retried later, and the two attempts never overlap."""
    coordinator, writer = await _coordinator(hass)
    _allow_writes(coordinator)
    current = 0
    peak = 0

    async def cleanup() -> None:
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        writer.cleanups += 1
        try:
            if writer.cleanups == 1:
                raise RuntimeError("cleanup failed")
            writer.rs485_on = False
        finally:
            current -= 1

    writer.async_cleanup_released_sequence = cleanup  # type: ignore[method-assign]
    coordinator.ensure_released_cleanup()
    await hass.async_block_till_done()
    if writer.cleanups == 1:
        assert coordinator._released_cleanup_unsub is not None
        assert coordinator._released_cleanup_task is None
        coordinator._on_released_cleanup_due(dt_util.utcnow())
        await hass.async_block_till_done()
    assert writer.cleanups == 2
    assert peak == 1
    assert coordinator._modbus_breaker_until_mono is None


async def test_unknown_stale_soc_sends_one_explicit_zero(hass, freezer) -> None:
    """Unknown device state with stale SoC sends one zero, then waits."""
    freezer.move_to("2026-06-01 12:00:00+00:00")
    coordinator, writer = await _coordinator(hass)
    _allow_writes(coordinator)
    _publish(hass, grid="0", soc="50", battery="0", force="stop")
    coordinator.set_pd_grid_deadband(0)
    coordinator.set_mode(const.MODE_SELF_CONSUMPTION)
    await hass.async_block_till_done()
    assert coordinator._session.ledger.accepted_w == 0
    assert writer.sends == []

    freezer.tick(timedelta(seconds=31))
    hass.states.async_set(FORCE, "unavailable")
    hass.states.async_remove(BATTERY)
    hass.states.async_remove(CHARGE)
    hass.states.async_remove(DISCHARGE)
    coordinator._session.pd.last_opportunity_mono = 1_000_000.0
    await coordinator._async_run_calculator()
    await hass.async_block_till_done()
    assert writer.sends == [0]
    assert coordinator._session._stale_soc_zero_latched is True

    await coordinator._async_run_calculator()
    await hass.async_block_till_done()
    assert writer.sends == [0]

    hass.states.async_set(SOC, "51")
    await coordinator._async_run_calculator()
    await hass.async_block_till_done()
    assert coordinator._session._stale_soc_zero_latched is False


async def test_failed_explicit_zero_can_be_retried(hass, freezer) -> None:
    """A failed safety zero is not latched, so the next calculation can send it."""
    freezer.move_to("2026-06-01 12:00:00+00:00")
    coordinator, writer = await _coordinator(hass)
    _allow_writes(coordinator)
    _publish(hass, grid="0", soc="50", battery="0", force="stop")
    coordinator.set_pd_grid_deadband(0)
    coordinator.set_mode(const.MODE_SELF_CONSUMPTION)
    await hass.async_block_till_done()

    async def send(setpoint_w: int) -> None:
        writer.sends.append(int(setpoint_w))
        if len(writer.sends) == 1:
            raise RuntimeError("zero failed")

    writer.async_send_setpoint = send  # type: ignore[method-assign]
    freezer.tick(timedelta(seconds=31))
    hass.states.async_set(FORCE, "unavailable")
    hass.states.async_remove(BATTERY)
    hass.states.async_remove(CHARGE)
    hass.states.async_remove(DISCHARGE)
    await coordinator._async_run_calculator()
    await hass.async_block_till_done()
    assert writer.sends == [0]
    assert coordinator._session._stale_soc_zero_latched is False
    assert coordinator._modbus_breaker_until_mono is None

    await coordinator._async_run_calculator()
    await hass.async_block_till_done()
    assert writer.sends == [0, 0]


async def test_open_breaker_drops_queued_command(hass, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure that opens the breaker does not send the command queued behind it."""
    monkeypatch.setattr(const, "MODBUS_FAILURE_ERROR_THRESHOLD", 1)
    coordinator, writer = await _coordinator(hass)
    _allow_writes(coordinator)
    writer.block = True
    fail_once = True

    async def send(setpoint_w: int) -> None:
        nonlocal fail_once
        writer.sends.append(int(setpoint_w))
        writer.started.set()
        if writer.block:
            writer.active += 1
            try:
                await writer.release.wait()
            finally:
                writer.active -= 1
        if fail_once:
            fail_once = False
            raise RuntimeError("transport")

    writer.async_send_setpoint = send  # type: ignore[method-assign]
    coordinator._dispatch_plan(_command(100))
    await asyncio.wait_for(writer.started.wait(), timeout=2)
    coordinator._dispatch_plan(_command(200))
    assert coordinator._queued_dispatch is not None
    assert coordinator._queued_dispatch.command_w == 200
    writer.release.set()
    task = coordinator._command_task
    assert task is not None
    await asyncio.wait_for(task, timeout=2)
    await hass.async_block_till_done()
    assert writer.sends == [100]
    assert coordinator._queued_dispatch is None
    assert coordinator._active_dispatch is None
    assert coordinator._modbus_breaker_until_mono is not None

    coordinator._dispatch_plan(_command(300))
    await hass.async_block_till_done()
    assert writer.sends == [100]

    coordinator._modbus_breaker_until_mono = None
    writer.block = False
    coordinator._dispatch_plan(_command(300))
    await hass.async_block_till_done()
    assert writer.sends == [100, 300]
