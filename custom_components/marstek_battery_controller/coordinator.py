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
    compute_base,
    compute_evening_peak_deadline,
    compute_latest_start_charge,
    compute_setpoint,
    effective_cap_threshold_w,
    energy_needed_for_evening_wh,
    minutes_until_evening_peak,
)
from .discovery import ResolvedEntities
from .modbus_writer import MarstekModbusWriter
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
    grid_entity_id: str
    cap_now_entity_id: str | None
    monthly_peak_entity_id: str | None
    use_internal_cap_now: bool


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
        self._modbus_failures = 0
        self._write_grace_until: datetime | None = None

        self._listeners: list[CALLBACK_TYPE] = []
        self._tick_unsub: CALLBACK_TYPE | None = None

        self._evening_peak_time = datetime.strptime("18:00", "%H:%M").time()
        self._passive_floor_time = datetime.strptime("13:00", "%H:%M").time()

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
        self._evening_min_soc = const.DEFAULT_EVENING_MIN_SOC
        self._evening_max_charge_power = const.DEFAULT_EVENING_MAX_CHARGE_POWER
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
    def evening_min_soc_value(self) -> float:
        """Evening target SoC (%)."""
        return self._evening_min_soc

    @property
    def evening_max_charge_power_value(self) -> float:
        """Evening boost charge power (W)."""
        return self._evening_max_charge_power

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

    def set_evening_min_soc(self, value: float) -> None:
        """Evening target floor."""
        self._evening_min_soc = value
        self._async_recalc()

    def set_evening_max_charge_power(self, value: float) -> None:
        """Grid boost charge power cap."""
        self._evening_max_charge_power = max(
            const.MIN_EVENING_CHARGE_POWER_W,
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

    def set_evening_peak_time(self, value: datetime | time) -> None:
        """Evening peak time-of-day."""
        if isinstance(value, datetime):
            self._evening_peak_time = value.time().replace(tzinfo=None)
        elif isinstance(value, time):
            self._evening_peak_time = value.replace(tzinfo=None)
        self._async_recalc()

    def set_passive_floor_time(self, value: datetime | time) -> None:
        """Passive protection start time."""
        if isinstance(value, datetime):
            self._passive_floor_time = value.time().replace(tzinfo=None)
        elif isinstance(value, time):
            self._passive_floor_time = value.replace(tzinfo=None)
        self._async_recalc()

    @property
    def evening_peak_time_value(self) -> time:
        """Evening peak as local time."""
        return self._evening_peak_time

    @property
    def passive_floor_time_value(self) -> time:
        """Passive floor protection start as local time."""
        return self._passive_floor_time

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
            "evening_min_soc": self._evening_min_soc,
            "evening_max_charge_power": self._evening_max_charge_power,
            "capacity_tariff_enabled": self._capacity_tariff_enabled,
            "max_desired_peak": self._max_desired_peak_w,
            "manual_target_soc": self._manual_target_soc,
            "manual_power": self._manual_power,
            "evening_peak_time": self._evening_peak_time.strftime("%H:%M"),
            "passive_floor_time": self._passive_floor_time.strftime("%H:%M"),
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
        for unsub in self._listeners:
            unsub()
        self._listeners.clear()
        await super().async_shutdown()

    def async_setup_listeners(self) -> None:
        """Subscribe to relevant entity state changes."""
        entities = [
            self.runtime.grid_entity_id,
            self.runtime.resolved.battery_soc,
            self.runtime.resolved.ac_power,
        ]
        if self.runtime.cap_now_entity_id:
            entities.append(self.runtime.cap_now_entity_id)
        if self.runtime.monthly_peak_entity_id:
            entities.append(self.runtime.monthly_peak_entity_id)

        @callback
        def _on_state(event: Event) -> None:
            self._async_recalc()
            self.hass.async_create_task(self._async_check_manual_auto_exit())

        self._listeners.append(
            async_track_state_change_event(self.hass, entities, _on_state)
        )

        self._write_grace_until = dt_util.utcnow() + timedelta(seconds=const.RESTART_WRITE_GRACE_S)
        _LOGGER.info(
            "Restart grace: Modbus writes deferred until %s",
            self._write_grace_until,
        )

        self._schedule_tick()
        if self._passive_floor_time >= self._evening_peak_time:
            _LOGGER.warning(
                "Passive floor protection start is not before evening peak; "
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

    async def _async_run_calculator(self) -> None:
        """Compute target setpoint from current HA state."""
        now = dt_util.now()
        hass = self.hass

        grid_ent = self.runtime.grid_entity_id
        soc_ent = self.runtime.resolved.battery_soc
        batt_ent = self.runtime.resolved.ac_power

        grid_st = hass.states.get(grid_ent)
        soc_st = hass.states.get(soc_ent)
        batt_st = hass.states.get(batt_ent)

        grid_ok = grid_st and grid_st.state not in ("unknown", "unavailable", None, "")
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
        grid_v = parse_optional_float_state(grid_st) if grid_ok else None
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

        deadline = compute_evening_peak_deadline(self._evening_peak_time, now)
        ls = compute_latest_start_charge(
            soc_now=current_soc,
            soc_target=self._evening_min_soc,
            p_max_w=self._evening_max_charge_power,
            batt_wh=self._battery_capacity_wh,
            peak_time=self._evening_peak_time,
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
            evening_min_soc=self._evening_min_soc,
            evening_max_charge_power_w=self._evening_max_charge_power,
            manual_target_soc=self._manual_target_soc,
            manual_power_w=self._manual_power,
            passive_floor_start=self._passive_floor_time,
            evening_peak_start=self._evening_peak_time,
            latest_start_charge=ls,
            evening_peak_deadline=deadline,
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

        try:
            await self.writer.async_send_setpoint(self.target_setpoint)
            self.last_sent_setpoint = self.target_setpoint
            self._modbus_failures = 0
            async_delete_issue(self.hass, const.DOMAIN, f"{const.DOMAIN}_{self.entry_id}_modbus")
            _LOGGER.debug(
                "Modbus write OK: target_setpoint=%s W",
                self.target_setpoint,
            )
        except Exception as err:
            self._modbus_failures += 1
            _LOGGER.warning(
                "Modbus write failed (%s consecutive): %s",
                self._modbus_failures,
                err,
            )
            if self._modbus_failures >= const.MODBUS_FAILURE_ERROR_THRESHOLD:
                _LOGGER.error(
                    "Modbus write failed %s times consecutively",
                    self._modbus_failures,
                )
                async_create_issue(
                    self.hass,
                    const.DOMAIN,
                    f"{const.DOMAIN}_{self.entry_id}_modbus",
                    breaks_invisibly=True,
                    is_fixable=False,
                    severity=IssueSeverity.ERROR,
                    translation_key="modbus_failures",
                    translation_placeholders={"count": str(self._modbus_failures)},
                )

    async def async_run_released_cleanup(self) -> None:
        """§6.1 cleanup when entering Released."""
        _LOGGER.info("Running Released cleanup sequence")
        try:
            await self.writer.async_cleanup_released_sequence()
        except Exception as err:
            _LOGGER.warning("Released cleanup failed: %s", err)
            raise
        self.last_sent_setpoint = None
        async_delete_issue(self.hass, const.DOMAIN, f"{const.DOMAIN}_{self.entry_id}_modbus")

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

    def diagnostic_energy_needed_evening_wh(self) -> float:
        """§14 energy Wh to evening floor."""
        soc_st = self.hass.states.get(self.runtime.resolved.battery_soc)
        cur = float(parse_optional_float_state(soc_st) or 0.0)
        return energy_needed_for_evening_wh(
            cur,
            self._evening_min_soc,
            self._battery_capacity_wh,
        )

    def diagnostic_minutes_to_peak(self) -> float:
        """Minutes until evening peak."""
        return minutes_until_evening_peak(self._evening_peak_time, dt_util.now())
