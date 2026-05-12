"""Coordinator: smoothing, calculator, Modbus writer, and timed write ticks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import logging
from typing import Any, Literal

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue, async_delete_issue
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import const
from .calculator import (
    CalculatorInputs,
    CalculatorOutput,
    compute_base,
    compute_latest_start_charge,
    compute_peak_window_deadline,
    compute_setpoint,
    effective_cap_threshold_w,
    energy_needed_for_reserve_wh,
    minutes_until_peak_window,
)
from .discovery import ResolvedEntities
from .modbus_writer import MarstekModbusWriter
from .homewizard_poller import HomeWizardPoller
from .smoothing import SlidingWindowMean
from .storage import MarstekStorage

_LOGGER = logging.getLogger(__name__)


def parse_optional_float_state(state: State | None) -> float | None:
    """Parse float from state or None if missing."""
    if state is None:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _time_from_datetime_entity(hass: HomeAssistant, entity_id: str | None) -> Any:
    """Read time portion from datetime entity state."""
    if not entity_id:
        return None
    st = hass.states.get(entity_id)
    if not st:
        return None
    # Datetime entities expose ISO string in state
    try:
        parsed = dt_util.parse_datetime(st.state)
        if parsed:
            return parsed.time().replace(tzinfo=None)
    except (TypeError, ValueError, AttributeError):
        pass
    return None


@dataclass
class CoordinatorRuntimeConfig:
    """Resolved wiring from config entry."""

    resolved: ResolvedEntities
    grid_entity_id: str | None
    cap_now_entity_id: str | None
    monthly_peak_entity_id: str | None
    use_internal_cap_now: bool
    grid_power_poller: HomeWizardPoller | None = None


class MarstekBatteryCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Orchestrates calculator output and Modbus writes."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry_id: str,
        runtime: CoordinatorRuntimeConfig,
        storage: MarstekStorage,
        initial_previous_mode: str | None,
    ) -> None:
        """Wire HA, entities, smoothing, and writer."""
        super().__init__(
            hass,
            _LOGGER,
            name="Marstek Battery Controller",
            update_interval=None,
        )
        self.entry_id = entry_id
        self.runtime = runtime
        self.storage = storage
        self.writer = MarstekModbusWriter(hass, runtime.resolved)

        self.previous_mode: str | None = initial_previous_mode

        self._send_interval_s: float = const.DEFAULT_SEND_INTERVAL
        self._grid_smooth_s: float = const.DEFAULT_GRID_SMOOTHING_WINDOW
        self._battery_smooth_s: float = const.DEFAULT_BATTERY_SMOOTHING_WINDOW

        self.grid_filter = SlidingWindowMean(self._grid_smooth_s)
        self.battery_filter = SlidingWindowMean(self._battery_smooth_s)
        self.cap_internal_filter = SlidingWindowMean(float(const.CAP_NOW_INTERNAL_WINDOW_S))

        self.target_setpoint: int = 0
        self.last_sent_setpoint: int | None = None
        self.last_calc_output: CalculatorOutput | None = None

        self._sensor_bad_since: dict[str, float | None] = {
            "grid": None,
            "soc": None,
            "battery_ac": None,
        }
        self._modbus_consecutive_failures = 0
        self._modbus_breaker_until_mono: float | None = None
        self._released_cleanup_task: asyncio.Task[None] | None = None
        self._write_grace_until: datetime | None = None

        # Must not shadow DataUpdateCoordinator._listeners (dict of entity update callbacks).
        self._state_change_unsubs: list[CALLBACK_TYPE] = []
        self._tick_unsub: CALLBACK_TYPE | None = None

        self._peak_window_time = datetime.strptime("18:00", "%H:%M").time()
        self._reserve_protection_time = datetime.strptime("13:00", "%H:%M").time()

        self._snapshot_grid_smoothed: float = 0.0
        self._snapshot_batt_smoothed: float = 0.0
        self._snapshot_cap_now: float = 0.0
        self._snapshot_latest_start: datetime | Literal["no_need"] | None = None

        self._capacity_tariff_enabled = const.DEFAULT_CAPACITY_TARIFF_ENABLED
        self._mode = const.DEFAULT_MODE
        self._min_soc = const.DEFAULT_MIN_SOC
        self._max_soc = const.DEFAULT_MAX_SOC
        self._max_battery_power = const.DEFAULT_MAX_BATTERY_POWER
        self._battery_capacity_wh = const.DEFAULT_BATTERY_CAPACITY_WH
        self._reserve_target_soc = const.DEFAULT_RESERVE_TARGET_SOC
        self._boost_charge_power = const.DEFAULT_BOOST_CHARGE_POWER
        self._max_desired_peak_w = const.DEFAULT_MAX_DESIRED_PEAK_W
        self._manual_target_soc = const.DEFAULT_MANUAL_TARGET_SOC
        self._manual_power = const.DEFAULT_MANUAL_POWER

    # —— Parameter setters (called by entity platforms) ————————————————

    def set_send_interval(self, value: float) -> None:
        """Update Modbus tick interval (seconds)."""
        self._send_interval_s = max(const.MIN_SEND_INTERVAL_S, min(const.MAX_SEND_INTERVAL_S, value))
        self._reschedule_tick()

    def set_grid_smoothing_window(self, value: float) -> None:
        """Resize grid smoothing window."""
        v = max(const.MIN_SMOOTHING_WINDOW_S, min(const.MAX_SMOOTHING_WINDOW_S, value))
        self._grid_smooth_s = v
        self.grid_filter.set_window_seconds(v)

    def set_battery_smoothing_window(self, value: float) -> None:
        """Resize battery smoothing window."""
        v = max(const.MIN_SMOOTHING_WINDOW_S, min(const.MAX_SMOOTHING_WINDOW_S, value))
        self._battery_smooth_s = v
        self.battery_filter.set_window_seconds(v)

    def set_capacity_tariff_enabled(self, value: bool) -> None:
        """Enable/disable capacity-tariff logic."""
        self._capacity_tariff_enabled = value
        self._async_recalc()

    def set_mode(self, mode: str) -> None:
        """Set operating mode (from UI)."""
        self._mode = mode
        self._async_recalc()

    @property
    def current_mode(self) -> str:
        """Current operating mode key (§6)."""
        return self._mode

    @property
    def capacity_tariff_enabled_flag(self) -> bool:
        """§7 capacity tariff switch state."""
        return self._capacity_tariff_enabled

    @property
    def min_soc_value(self) -> float:
        """Min SoC (%)."""
        return self._min_soc

    @property
    def max_soc_value(self) -> float:
        """Max SoC (%)."""
        return self._max_soc

    @property
    def max_battery_power_value(self) -> float:
        """Max battery power (W)."""
        return self._max_battery_power

    @property
    def send_interval_value(self) -> float:
        """Send interval (s)."""
        return self._send_interval_s

    @property
    def grid_smoothing_window_value(self) -> float:
        """Grid smoothing window (s)."""
        return self._grid_smooth_s

    @property
    def battery_smoothing_window_value(self) -> float:
        """Battery smoothing window (s)."""
        return self._battery_smooth_s

    @property
    def battery_capacity_wh_value(self) -> float:
        """Battery capacity (Wh)."""
        return self._battery_capacity_wh

    @property
    def reserve_target_soc_value(self) -> float:
        """Reserve target SoC (%)."""
        return self._reserve_target_soc

    @property
    def boost_charge_power_value(self) -> float:
        """Boost charge power from grid (W)."""
        return self._boost_charge_power

    @property
    def max_desired_peak_value(self) -> float:
        """Max desired 15-min peak (W)."""
        return self._max_desired_peak_w

    @property
    def manual_target_soc_value(self) -> float:
        """Manual target SoC (%)."""
        return self._manual_target_soc

    @property
    def manual_power_value(self) -> float:
        """Manual power magnitude (W)."""
        return self._manual_power

    def set_min_soc(self, value: float) -> None:
        """Set discharge floor."""
        self._min_soc = value
        self._async_recalc()

    def set_max_soc(self, value: float) -> None:
        """Set charge ceiling."""
        self._max_soc = value
        self._async_recalc()

    def set_max_battery_power(self, value: float) -> None:
        """Clamp max power."""
        self._max_battery_power = max(
            const.MIN_BATTERY_POWER_W, min(const.MAX_BATTERY_POWER_LIMIT_W, value)
        )
        self._async_recalc()

    def set_battery_capacity_wh(self, value: float) -> None:
        """Battery nameplate capacity."""
        self._battery_capacity_wh = max(
            const.MIN_BATTERY_CAPACITY_WH, min(const.MAX_BATTERY_CAPACITY_WH, value)
        )
        self._async_recalc()

    def set_reserve_target_soc(self, value: float) -> None:
        """Reserve target SoC for peak window."""
        self._reserve_target_soc = value
        self._async_recalc()

    def set_boost_charge_power(self, value: float) -> None:
        """Grid boost charge power cap."""
        self._boost_charge_power = max(
            const.MIN_BOOST_CHARGE_POWER_W,
            min(value, self._max_battery_power),
        )
        self._async_recalc()

    def set_max_desired_peak(self, value: float) -> None:
        """User capacity-tariff ceiling."""
        self._max_desired_peak_w = max(
            const.MIN_MAX_DESIRED_PEAK_W, min(const.MAX_MAX_DESIRED_PEAK_W, value)
        )
        self._async_recalc()

    def set_manual_target_soc(self, value: float) -> None:
        """Manual target SoC."""
        self._manual_target_soc = max(const.MIN_SOC_PCT, min(const.MAX_SOC_PCT, value))
        self._async_recalc()

    def set_manual_power(self, value: float) -> None:
        """Manual charge/discharge power."""
        self._manual_power = max(
            const.MIN_BATTERY_POWER_W, min(value, self._max_battery_power)
        )
        self._async_recalc()

    def set_peak_window_time(self, value: datetime | time) -> None:
        """Peak window start time-of-day."""
        if isinstance(value, datetime):
            self._peak_window_time = value.time().replace(tzinfo=None)
        elif isinstance(value, time):
            self._peak_window_time = value.replace(tzinfo=None)
        self._async_recalc()

    def set_reserve_protection_time(self, value: datetime | time) -> None:
        """Reserve protection window start time."""
        if isinstance(value, datetime):
            self._reserve_protection_time = value.time().replace(tzinfo=None)
        elif isinstance(value, time):
            self._reserve_protection_time = value.replace(tzinfo=None)
        self._async_recalc()

    @property
    def peak_window_time_value(self) -> time:
        """Peak window start as local time."""
        return self._peak_window_time

    @property
    def reserve_protection_time_value(self) -> time:
        """Reserve protection start as local time."""
        return self._reserve_protection_time

    @property
    def snapshot_grid_smoothed(self) -> float:
        """Last computed smoothed grid power (W)."""
        return self._snapshot_grid_smoothed

    @property
    def snapshot_batt_smoothed(self) -> float:
        """Last computed smoothed battery AC power (W)."""
        return self._snapshot_batt_smoothed

    @property
    def snapshot_cap_now(self) -> float:
        """Last computed cap_now (user sensor or internal rolling mean)."""
        return self._snapshot_cap_now

    @property
    def snapshot_latest_start_charge(self) -> datetime | Literal["no_need"] | None:
        """Latest-start charge diagnostic (§10)."""
        return self._snapshot_latest_start

    def as_options_dict(self) -> dict[str, Any]:
        """Serialize coordinator parameters for config entry options."""
        return {
            "mode": self._mode,
            "min_soc": self._min_soc,
            "max_soc": self._max_soc,
            "max_battery_power": self._max_battery_power,
            "send_interval": self._send_interval_s,
            "grid_smoothing": self._grid_smooth_s,
            "battery_smoothing": self._battery_smooth_s,
            "battery_capacity": self._battery_capacity_wh,
            "evening_min_soc": self._reserve_target_soc,
            "evening_max_charge_power": self._boost_charge_power,
            "capacity_tariff_enabled": self._capacity_tariff_enabled,
            "max_desired_peak": self._max_desired_peak_w,
            "manual_target_soc": self._manual_target_soc,
            "manual_power": self._manual_power,
            "evening_peak_time": self._peak_window_time.strftime("%H:%M"),
            "passive_floor_time": self._reserve_protection_time.strftime("%H:%M"),
        }

    def persist_entry_options(self, hass: HomeAssistant, entry_id: str) -> None:
        """Merge coordinator parameters into config entry options for restart persistence."""
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, **self.as_options_dict()},
        )

    # —— Lifecycle —————————————————————————————————————————————————————

    async def async_shutdown(self) -> None:
        """Detach listeners."""
        self._cancel_tick()
        if self._released_cleanup_task and not self._released_cleanup_task.done():
            self._released_cleanup_task.cancel()
            try:
                await self._released_cleanup_task
            except asyncio.CancelledError:
                pass
        self._released_cleanup_task = None
        for unsub in self._state_change_unsubs:
            unsub()
        self._state_change_unsubs.clear()
        await super().async_shutdown()

    def async_setup_listeners(self) -> None:
        """Subscribe to relevant entity state changes."""
        entities: list[str] = []
        if self.runtime.grid_entity_id and self.runtime.grid_power_poller is None:
            entities.append(self.runtime.grid_entity_id)
        entities.extend(
            [
                self.runtime.resolved.battery_soc,
                self.runtime.resolved.ac_power,
            ]
        )
        if self.runtime.cap_now_entity_id:
            entities.append(self.runtime.cap_now_entity_id)
        if self.runtime.monthly_peak_entity_id:
            entities.append(self.runtime.monthly_peak_entity_id)

        @callback
        def _on_state(event: Event) -> None:
            self._async_recalc()
            self.hass.async_create_task(self._async_check_manual_auto_exit())

        self._state_change_unsubs.append(
            async_track_state_change_event(self.hass, entities, _on_state)
        )

        self._write_grace_until = dt_util.utcnow() + timedelta(seconds=const.RESTART_WRITE_GRACE_S)
        _LOGGER.info(
            "Restart grace: Modbus writes deferred until %s",
            self._write_grace_until,
        )

        self._schedule_tick()
        if self._reserve_protection_time >= self._peak_window_time:
            _LOGGER.warning(
                "Reserve protection start is not before peak window start; "
                "protection window is empty (§16)",
            )
        self.hass.async_create_task(self._async_run_calculator())

    async def _async_check_manual_auto_exit(self) -> None:
        """Restore previous_mode when manual target reached."""
        if self._mode != const.MODE_MANUAL:
            return
        soc_st = self.hass.states.get(self.runtime.resolved.battery_soc)
        cur = parse_optional_float_state(soc_st)
        if cur is None:
            return
        if round(cur) != round(self._manual_target_soc):
            return
        prev = self.previous_mode or const.MODE_RELEASED
        _LOGGER.info("Manual auto-exit: restoring mode %s", prev)
        self._mode = prev
        self.previous_mode = None
        await self.storage.async_save_previous_mode(None)
        self.persist_entry_options(self.hass, self.entry_id)
        self.async_update_listeners()

    async def _async_update_data(self) -> dict[str, Any]:
        """DataUpdateCoordinator API — refresh computed state."""
        await self._async_run_calculator()
        return {
            "target_setpoint": self.target_setpoint,
            "last_sent_setpoint": self.last_sent_setpoint,
        }

    def _cancel_tick(self) -> None:
        if self._tick_unsub:
            self._tick_unsub()
            self._tick_unsub = None

    def _schedule_tick(self) -> None:
        """Periodic write tick."""
        self._cancel_tick()

        @callback
        def _tick(_now: datetime) -> None:
            self.hass.async_create_task(self._async_tick_writes())

        self._tick_unsub = async_track_time_interval(
            self.hass,
            _tick,
            timedelta(seconds=max(1.0, self._send_interval_s)),
        )

    def _reschedule_tick(self) -> None:
        self._schedule_tick()

    @callback
    def _async_recalc(self) -> None:
        """Run calculator on next loop iteration."""
        self.hass.async_create_task(self._async_run_calculator())

    def _on_grid_sample(self, value_w: float, monotonic_ts: float) -> None:
        """Called by HomeWizard poller after each successful read."""
        self._async_recalc()

    async def _async_run_calculator(self) -> None:
        """Compute target setpoint from current HA state."""
        now = dt_util.now()
        hass = self.hass

        soc_ent = self.runtime.resolved.battery_soc
        batt_ent = self.runtime.resolved.ac_power

        poller = self.runtime.grid_power_poller
        grid_st: State | None
        grid_v: float | None
        if poller is not None:
            grid_v = poller.latest_w
            grid_ok = poller.is_fresh()
            grid_st = None
        else:
            grid_ent = self.runtime.grid_entity_id
            grid_st = hass.states.get(grid_ent) if grid_ent else None
            grid_ok = grid_st and grid_st.state not in ("unknown", "unavailable", None, "")
            grid_v = parse_optional_float_state(grid_st) if grid_ok else None

        soc_st = hass.states.get(soc_ent)
        batt_st = hass.states.get(batt_ent)

        soc_ok = soc_st and soc_st.state not in ("unknown", "unavailable", None, "")
        batt_ok = batt_st and batt_st.state not in ("unknown", "unavailable", None, "")

        mono = asyncio.get_running_loop().time()

        def _mark_bad(key: str, ok: bool) -> None:
            if ok:
                self._sensor_bad_since[key] = None
                return
            if self._sensor_bad_since[key] is None:
                self._sensor_bad_since[key] = mono
                _LOGGER.warning("Sensor for %s became unavailable", key)
            elif mono - float(self._sensor_bad_since[key] or 0) >= const.SENSOR_UNAVAILABLE_RELEASE_S:
                if self._mode != const.MODE_RELEASED:
                    _LOGGER.error(
                        "Forcing Released after prolonged unavailability: %s",
                        key,
                    )
                    async_create_issue(
                        hass,
                        const.DOMAIN,
                        f"{const.DOMAIN}_{self.entry_id}_sensor",
                        breaks_invisibly=False,
                        is_fixable=False,
                        severity=IssueSeverity.WARNING,
                        translation_key="sensor_unavailable",
                        translation_placeholders={"name": key},
                    )
                    self._mode = const.MODE_RELEASED
                    self.async_update_listeners()

        _mark_bad("grid", bool(grid_ok))
        _mark_bad("soc", bool(soc_ok))
        _mark_bad("battery_ac", bool(batt_ok))

        ts = now.timestamp()
        if grid_v is not None:
            self.grid_filter.push(ts, grid_v)
            self.cap_internal_filter.push(ts, grid_v)

        batt_v = parse_optional_float_state(batt_st) if batt_ok else None
        if batt_v is not None:
            self.battery_filter.push(ts, batt_v)

        grid_smoothed = self.grid_filter.mean(
            ts, fallback=grid_v if grid_v is not None else 0.0
        )
        batt_smoothed = (
            self.battery_filter.mean(ts, fallback=batt_v if batt_v is not None else 0.0)
            if batt_ok
            else 0.0
        )

        cap_now: float
        if self.runtime.cap_now_entity_id:
            cst = hass.states.get(self.runtime.cap_now_entity_id)
            cap_now = float(parse_optional_float_state(cst) or 0.0)
        else:
            cap_now = self.cap_internal_filter.mean(ts, fallback=grid_v or 0.0)

        current_soc = float(parse_optional_float_state(soc_st) or 0.0)

        monthly_peak = None
        monthly_ok = False
        if self.runtime.monthly_peak_entity_id:
            mst = hass.states.get(self.runtime.monthly_peak_entity_id)
            monthly_peak = parse_optional_float_state(mst)
            monthly_ok = mst is not None and mst.state not in (
                "unknown",
                "unavailable",
            )

        deadline = compute_peak_window_deadline(self._peak_window_time, now)
        ls = compute_latest_start_charge(
            soc_now=current_soc,
            soc_target=self._reserve_target_soc,
            p_max_w=self._boost_charge_power,
            batt_wh=self._battery_capacity_wh,
            peak_time=self._peak_window_time,
            now=now,
        )

        inp = CalculatorInputs(
            mode=self._mode,
            grid_smoothed=grid_smoothed,
            battery_smoothed=batt_smoothed,
            current_soc=current_soc,
            grid_sensor_ok=bool(grid_ok),
            soc_sensor_ok=bool(soc_ok),
            battery_sensor_ok=bool(batt_ok),
            cap_now=cap_now,
            monthly_peak_w=monthly_peak,
            monthly_peak_ok=monthly_ok,
            capacity_tariff_enabled=self._capacity_tariff_enabled,
            max_desired_peak_w=self._max_desired_peak_w,
            min_soc=self._min_soc,
            max_soc=self._max_soc,
            max_battery_power_w=self._max_battery_power,
            reserve_target_soc=self._reserve_target_soc,
            boost_charge_power_w=self._boost_charge_power,
            manual_target_soc=self._manual_target_soc,
            manual_power_w=self._manual_power,
            reserve_protection_start=self._reserve_protection_time,
            peak_window_start=self._peak_window_time,
            latest_start_charge=ls,
            peak_window_deadline=deadline,
            now=now,
        )

        self._snapshot_grid_smoothed = grid_smoothed
        self._snapshot_batt_smoothed = batt_smoothed
        self._snapshot_cap_now = cap_now
        self._snapshot_latest_start = ls

        base_preview = compute_base(grid_smoothed, batt_smoothed)
        out = compute_setpoint(inp)
        self.last_calc_output = out
        _LOGGER.debug(
            "setpoint: mode=%s grid=%.1f batt=%.1f base=%.1f -> out_w=%s state=%s reason=%s",
            self._mode,
            grid_smoothed,
            batt_smoothed,
            base_preview,
            out.setpoint_w,
            out.operating_state,
            out.reason_code,
        )

        if out.setpoint_w is None:
            self.async_update_listeners()
            return

        self.target_setpoint = out.setpoint_w
        self.async_update_listeners()

    def _modbus_breaker_blocks(self) -> bool:
        """Return True if Modbus sequences must be skipped (cooldown active)."""
        until = self._modbus_breaker_until_mono
        if until is None:
            return False
        loop_ts = asyncio.get_running_loop().time()
        if loop_ts >= until:
            self._modbus_breaker_until_mono = None
            return False
        return True

    def _record_modbus_sequence_success(self) -> None:
        """Reset failure streak and close circuit after any successful full sequence."""
        self._modbus_consecutive_failures = 0
        self._modbus_breaker_until_mono = None
        async_delete_issue(self.hass, const.DOMAIN, f"{const.DOMAIN}_{self.entry_id}_modbus")

    def _record_modbus_sequence_failure(self, err: BaseException | None) -> None:
        """Count one failed full Modbus sequence; trip shared breaker after threshold."""
        self._modbus_consecutive_failures += 1
        _LOGGER.warning(
            "Modbus sequence failed (%s/%s): %s",
            self._modbus_consecutive_failures,
            const.MODBUS_FAILURE_ERROR_THRESHOLD,
            err,
        )
        if self._modbus_consecutive_failures < const.MODBUS_FAILURE_ERROR_THRESHOLD:
            return
        self._modbus_consecutive_failures = 0
        self._modbus_breaker_until_mono = (
            asyncio.get_running_loop().time() + const.MODBUS_CIRCUIT_COOLDOWN_S
        )
        _LOGGER.error(
            "Modbus circuit breaker open for %ss (no writes/cleanup until cooldown)",
            const.MODBUS_CIRCUIT_COOLDOWN_S,
        )
        async_create_issue(
            self.hass,
            const.DOMAIN,
            f"{const.DOMAIN}_{self.entry_id}_modbus",
            breaks_invisibly=True,
            is_fixable=False,
            severity=IssueSeverity.ERROR,
            translation_key="modbus_failures",
            translation_placeholders={"count": str(const.MODBUS_FAILURE_ERROR_THRESHOLD)},
        )

    async def _async_tick_writes(self) -> None:
        """Send Modbus if target differs from last sent."""
        if self._write_grace_until:
            if dt_util.utcnow() >= self._write_grace_until:
                _LOGGER.info(
                    "Restart write grace elapsed; Modbus writes enabled",
                )
                self._write_grace_until = None
            else:
                return

        if self._mode == const.MODE_RELEASED:
            return

        if (
            self.last_sent_setpoint is not None
            and self.last_sent_setpoint == self.target_setpoint
        ):
            return

        if self._modbus_breaker_blocks():
            return

        try:
            await asyncio.wait_for(
                self.writer.async_send_setpoint(self.target_setpoint),
                timeout=const.MODBUS_SEQUENCE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Modbus write timed out after %ss",
                const.MODBUS_SEQUENCE_TIMEOUT_S,
            )
            self._record_modbus_sequence_failure(None)
            return
        except Exception as err:
            self._record_modbus_sequence_failure(err)
            return

        self.last_sent_setpoint = self.target_setpoint
        self._record_modbus_sequence_success()
        _LOGGER.debug(
            "Modbus write OK: target_setpoint=%s W",
            self.target_setpoint,
        )

    async def async_schedule_released_cleanup(self) -> None:
        """Cancel any in-flight Released cleanup and run cleanup when the circuit allows."""
        if self._released_cleanup_task and not self._released_cleanup_task.done():
            self._released_cleanup_task.cancel()
            try:
                await self._released_cleanup_task
            except asyncio.CancelledError:
                pass
        self._released_cleanup_task = self.hass.async_create_task(
            self._async_released_cleanup_when_able()
        )

    async def _async_released_cleanup_when_able(self) -> None:
        """Wait out Modbus cooldown, then run full Released cleanup with timeout."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                until = self._modbus_breaker_until_mono
                if until is None:
                    break
                now = loop.time()
                if now >= until:
                    self._modbus_breaker_until_mono = None
                    break
                await asyncio.sleep(min(1.0, max(0.05, until - now)))

            if self._mode != const.MODE_RELEASED:
                _LOGGER.debug("Released cleanup skipped: mode is %s", self._mode)
                return

            _LOGGER.info("Running Released cleanup sequence")
            await asyncio.wait_for(
                self.writer.async_cleanup_released_sequence(),
                timeout=const.MODBUS_SEQUENCE_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Released cleanup timed out after %ss",
                const.MODBUS_SEQUENCE_TIMEOUT_S,
            )
            self._record_modbus_sequence_failure(None)
            self.last_sent_setpoint = None
            self.async_update_listeners()
            return
        except Exception as err:
            _LOGGER.warning("Released cleanup failed: %s", err)
            self._record_modbus_sequence_failure(err)
            self.last_sent_setpoint = None
            self.async_update_listeners()
            return

        self._record_modbus_sequence_success()
        self.last_sent_setpoint = None
        self.async_update_listeners()

    async def async_run_released_cleanup(self) -> None:
        """Await Released cleanup (tests); UI uses async_schedule_released_cleanup."""
        await self._async_released_cleanup_when_able()

    def effective_cap_w(self) -> float:
        """Expose §9.2 threshold for diagnostics."""
        hass = self.hass
        monthly_peak = None
        monthly_ok = False
        if self.runtime.monthly_peak_entity_id:
            mst = hass.states.get(self.runtime.monthly_peak_entity_id)
            monthly_peak = parse_optional_float_state(mst)
            monthly_ok = mst is not None and mst.state not in (
                "unknown",
                "unavailable",
            )
        return effective_cap_threshold_w(
            self._max_desired_peak_w,
            monthly_peak,
            monthly_ok,
        )

    def diagnostic_energy_needed_reserve_wh(self) -> float:
        """§14 energy Wh to reserve target."""
        soc_st = self.hass.states.get(self.runtime.resolved.battery_soc)
        cur = float(parse_optional_float_state(soc_st) or 0.0)
        return energy_needed_for_reserve_wh(
            cur,
            self._reserve_target_soc,
            self._battery_capacity_wh,
        )

    def diagnostic_minutes_to_peak(self) -> float:
        """Minutes until peak window start."""
        return minutes_until_peak_window(self._peak_window_time, dt_util.now())
