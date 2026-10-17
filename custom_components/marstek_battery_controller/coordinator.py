"""Coordinator: PD battery control, diagnostics, and Modbus writes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import logging
import time as time_mod
from typing import Any, Literal, Mapping

from homeassistant.const import ATTR_RESTORED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue, async_delete_issue
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import const
from .battery_transport import deliver_command
from .calculator import (
    CalculatorOutput,
    compute_latest_start_charge,
    compute_peak_window_deadline,
    effective_cap_threshold_w,
    energy_needed_for_reserve_wh,
    minutes_until_peak_window,
)
from .modbus_writer import MarstekModbusWriter
from .pd_session import (
    BatteryControlSession,
    ChannelSample,
    ControlPlan,
    ReportedObservation,
    SessionSettings,
    normalize_pd_grid_deadband,
    reported_command_w,
)
from .publications import DEFAULT_BATTERY_FRESH_S, DEFAULT_SOC_FRESH_S
from .discovery import ResolvedEntities
from .ev_input import effective_exclude_ev, ev_channel_sample, requested_exclude_ev
from .homewizard_poller import HomeWizardPoller
from .smoothing import SlidingWindowMean
from .storage import MarstekStorage

_LOGGER = logging.getLogger(__name__)


def _create_issue(hass: HomeAssistant, domain: str, issue_id: str, **kwargs: Any) -> None:
    """Create a repairs issue, retrying without arguments this core does not accept."""
    try:
        async_create_issue(hass, domain, issue_id, **kwargs)
    except TypeError:
        kwargs.pop("breaks_invisibly", None)
        async_create_issue(hass, domain, issue_id, **kwargs)


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
    ev_power_entity_id: str | None = None
    grid_power_poller: HomeWizardPoller | None = None


class _Dispatch:
    """One reserved battery write. Pending work lives here, not in a second submit."""

    __slots__ = ("command_w", "safety", "reassert", "track_pd")

    def __init__(
        self,
        command_w: int,
        *,
        safety: bool,
        reassert: bool,
        track_pd: bool,
    ) -> None:
        self.command_w = int(command_w)
        self.safety = safety
        self.reassert = reassert
        self.track_pd = track_pd


class _WriterTransport:
    """Adapt the existing marstek_modbus writer to the PD transport contract."""

    def __init__(self, writer: MarstekModbusWriter) -> None:
        self._writer = writer

    async def async_send_command(self, command_w: int) -> None:
        """Apply one signed setpoint. Positive discharges, negative charges."""
        await self._writer.async_send_setpoint(command_w)

    async def async_release(self) -> None:
        """Return the battery to its own control."""
        await self._writer.async_cleanup_released_sequence()


class MarstekBatteryCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Orchestrates PD battery control and Modbus writes."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry_id: str,
        runtime: CoordinatorRuntimeConfig,
        storage: MarstekStorage,
        initial_previous_mode: str | None,
        ha_process_started_utc: datetime | None = None,
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
        self._released_cleanup_unsub: CALLBACK_TYPE | None = None
        self._command_task: asyncio.Task[None] | None = None
        self._active_dispatch: _Dispatch | None = None
        self._queued_dispatch: _Dispatch | None = None
        self._shutting_down = False
        self._release_cleanup_owned = False
        self._cleanup_physical = False
        self._mode_after_cleanup: str | None = None
        self._inherited_released_uncertainty = False
        self._session = BatteryControlSession()
        self._pd_deadband_w = const.DEFAULT_PD_GRID_DEADBAND_W
        # Before any options-driven recalc or the first coordinator refresh.
        self._write_grace_until: datetime | None = dt_util.utcnow() + timedelta(
            seconds=const.RESTART_WRITE_GRACE_S
        )

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
        self._exclude_ev_requested = False
        self._exclude_ev_persisted = False
        # Injected by setup from the Home Assistant process start. None rejects
        # EV samples, which is safe until setup supplies the timestamp.
        self.ha_process_started_utc = ha_process_started_utc
        self._mode = const.DEFAULT_MODE
        self._inherited_released_uncertainty = self._mode == const.MODE_RELEASED
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
        """Minimum gap between ordinary PD opportunities. Not a write heartbeat."""
        self._send_interval_s = max(const.MIN_SEND_INTERVAL_S, min(const.MAX_SEND_INTERVAL_S, value))
        self._reschedule_tick()

    def set_pd_grid_deadband(self, value: float) -> None:
        """Allowed grid error before PD acts. 0 W disables the deadband."""
        self._pd_deadband_w = normalize_pd_grid_deadband(value)
        self._async_recalc()

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

    def configure_startup_ev(self, entity_id: str | None, options: Mapping[str, Any]) -> None:
        """Apply the sensor and requested exclusion before the first refresh.

        This does not calculate. Startup calls it before any await so the first
        control step already sees the configured policy.
        """
        self.runtime.ev_power_entity_id = entity_id
        self._exclude_ev_requested = requested_exclude_ev(options, entity_id)
        self._exclude_ev_persisted = const.CONF_EXCLUDE_EV in options or entity_id is not None

    def set_exclude_ev_requested(self, value: bool, *, recalc: bool = True) -> None:
        """Store the requested Exclude EV choice and recalculate."""
        self._exclude_ev_requested = bool(value)
        self._exclude_ev_persisted = True
        if recalc:
            self._async_recalc()

    def set_mode(self, mode: str) -> None:
        """Set operating mode. Entering Released schedules cleanup once."""
        if mode == const.MODE_RELEASED:
            self._mode_after_cleanup = None
            self._enter_released()
            return
        if self._released_cleanup_active():
            # The cleanup task owns the transition, including its wait for an
            # in-flight command. Keep the newest mode and do not queue a write.
            self._mode_after_cleanup = mode
            self._queued_dispatch = None
            return
        self._leave_released_now(mode)

    def _released_cleanup_active(self) -> bool:
        """True for the whole Released cleanup task, before and during Modbus."""
        task = self._released_cleanup_task
        return task is not None and not task.done()

    def _release_cleanup_exclusivity(self) -> None:
        """Let a handoff dispatch. A deferred timer is not a running cleanup."""
        if self._released_cleanup_task is asyncio.current_task():
            self._released_cleanup_task = None

    def _leave_released_now(self, mode: str) -> None:
        """Leave Released when no physical cleanup is on the wire."""
        if self._mode == const.MODE_RELEASED:
            self._release_cleanup_owned = False
            self._inherited_released_uncertainty = False
            self._cancel_released_wait()
        self._mode = mode
        self._async_recalc()

    def ensure_released_cleanup(self) -> None:
        """Schedule Released cleanup when startup or options left the mode Released."""
        if self.current_mode == const.MODE_RELEASED:
            self._schedule_released_cleanup()

    def _enter_released(self) -> None:
        """Switch to Released and schedule physical cleanup at most once per entry."""
        self._mode = const.MODE_RELEASED
        self._async_recalc()
        self._schedule_released_cleanup()

    def _schedule_released_cleanup(self) -> None:
        """Start or defer Released cleanup. A second entry does not start another sequence."""
        if self._shutting_down or self._release_cleanup_owned:
            return
        if self._released_cleanup_task is not None and not self._released_cleanup_task.done():
            self._release_cleanup_owned = True
            return
        self._release_cleanup_owned = True
        self._arm_released_cleanup()

    def _arm_released_cleanup(self) -> None:
        """Run cleanup now, or defer it until restart grace and the breaker are clear."""
        if self._shutting_down or not self._release_cleanup_owned:
            return
        delay = self._cleanup_delay_s()
        if delay > 0:
            if self._released_cleanup_unsub is not None:
                return
            self._released_cleanup_unsub = async_call_later(
                self.hass,
                delay,
                self._on_released_cleanup_due,
            )
            return
        if self._released_cleanup_task is not None and not self._released_cleanup_task.done():
            return
        self._released_cleanup_task = self.hass.async_create_task(
            self._async_released_cleanup_when_able(),
            eager_start=False,
        )

    @callback
    def _on_released_cleanup_due(self, _now: datetime) -> None:
        """Grace or breaker wait elapsed; run cleanup or defer again."""
        self._released_cleanup_unsub = None
        self._arm_released_cleanup()

    def _cleanup_delay_s(self) -> float:
        """Seconds to wait before a Released Modbus sequence may start."""
        delay = 0.0
        if self._write_grace_until is not None:
            remaining = (self._write_grace_until - dt_util.utcnow()).total_seconds()
            if remaining > 0:
                delay = remaining
            else:
                self._write_grace_until = None
        until = self._modbus_breaker_until_mono
        if until is not None:
            try:
                loop_now = asyncio.get_running_loop().time()
            except RuntimeError:
                loop_now = time_mod.monotonic()
            remaining_breaker = until - loop_now
            if remaining_breaker > 0:
                delay = max(delay, remaining_breaker)
            else:
                self._modbus_breaker_until_mono = None
        return delay

    def _cancel_released_wait(self) -> None:
        """Drop a deferred Released cleanup that has not started Modbus yet."""
        if self._released_cleanup_unsub is not None:
            self._released_cleanup_unsub()
            self._released_cleanup_unsub = None

    @property
    def current_mode(self) -> str:
        """Current operating mode key (§6)."""
        return self._mode

    @property
    def exclude_ev_requested(self) -> bool:
        """The stored Exclude EV choice, whether or not a sensor is configured."""
        return self._exclude_ev_requested

    @property
    def ev_sensor_configured(self) -> bool:
        """True when an EV charging-power entity id is configured."""
        return self.runtime.ev_power_entity_id is not None

    @property
    def exclude_ev_effective(self) -> bool:
        """Requested exclusion while an EV sensor is configured."""
        return effective_exclude_ev(self._exclude_ev_requested, self.runtime.ev_power_entity_id)

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
        """Minimum ordinary control interval (s)."""
        return self._send_interval_s

    @property
    def pd_grid_deadband_value(self) -> float:
        """PD grid deadband (W)."""
        return self._pd_deadband_w

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
        payload = {
            "mode": self._mode,
            "min_soc": self._min_soc,
            "max_soc": self._max_soc,
            "max_battery_power": self._max_battery_power,
            "send_interval": self._send_interval_s,
            "pd_grid_deadband": self._pd_deadband_w,
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
        if self._exclude_ev_persisted:
            payload[const.CONF_EXCLUDE_EV] = self._exclude_ev_requested
        return payload

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
        """Stop dispatch, finish or cancel Modbus work, then detach listeners."""
        self._shutting_down = True
        self._cancel_tick()
        command_task = self._command_task
        if command_task is not None and not command_task.done():
            command_task.cancel()
            try:
                await command_task
            except asyncio.CancelledError:
                pass
        self._command_task = None
        self._active_dispatch = None
        self._queued_dispatch = None
        self._cancel_released_wait()
        cleanup_task = self._released_cleanup_task
        if cleanup_task is not None and not cleanup_task.done():
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        self._released_cleanup_task = None
        if self._owns_forced_battery():
            try:
                await asyncio.wait_for(
                    self.writer.async_cleanup_released_sequence(),
                    timeout=const.MODBUS_SEQUENCE_TIMEOUT_S,
                )
                self._session.note_release_finished(accepted=True)
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Unload cleanup timed out after %ss",
                    const.MODBUS_SEQUENCE_TIMEOUT_S,
                )
                self._session.note_release_finished(accepted=False)
            except Exception as err:
                _LOGGER.warning("Unload cleanup failed: %s", err)
                self._session.note_release_finished(accepted=False)
        for unsub in self._state_change_unsubs:
            unsub()
        self._state_change_unsubs.clear()
        await super().async_shutdown()

    def _owns_forced_battery(self) -> bool:
        """True when unload must still neutralize forced battery control.

        A scheduled Released cleanup is ownership even if RS485 looks off and
        the accepted command is already zero. Startup in Released inherits
        that uncertainty from the previous process.
        """
        if self._release_cleanup_owned or self._released_cleanup_unsub is not None:
            return True
        if self._cleanup_physical:
            return True
        if self._inherited_released_uncertainty and self._mode == const.MODE_RELEASED:
            return True
        if self.writer.rs485_on:
            return True
        if self._session.ledger.delivery_uncertain or self._session.ledger.in_flight_w is not None:
            return True
        if self._session.ledger.accepted_w != 0:
            return True
        return self._mode != const.MODE_RELEASED

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
        ev_entity_id = self.runtime.ev_power_entity_id
        if ev_entity_id and ev_entity_id not in entities:
            entities.append(ev_entity_id)

        @callback
        def _on_state(event: Event) -> None:
            self._async_recalc()
            self.hass.async_create_task(self._async_check_manual_auto_exit())

        self._state_change_unsubs.append(
            async_track_state_change_event(self.hass, entities, _on_state)
        )
        if ev_entity_id:
            # State changes and same-value reports are separate Home Assistant
            # events, so one publication schedules one calculation.
            self._listen_ev_reports(ev_entity_id)

        self._schedule_tick()
        if self._write_grace_until is not None:
            _LOGGER.info(
                "Restart grace: Modbus writes deferred until %s",
                self._write_grace_until,
            )
        if self._reserve_protection_time >= self._peak_window_time:
            _LOGGER.warning(
                "Reserve protection start is not before peak window start; "
                "protection window is empty (§16)",
            )
        self.hass.async_create_task(self._async_run_calculator())

    def _listen_ev_reports(self, entity_id: str) -> None:
        """Recalculate when the EV sensor reports the same value again.

        The listener is filtered to this entity. Unload uses the same
        unsubscribe list as the state-change listeners.
        """
        from homeassistant.helpers import event as ha_event

        tracker = getattr(ha_event, "async_track_state_report_event", None)
        if tracker is None:
            _LOGGER.debug(
                "Home Assistant has no state-reported listener; an unchanged EV value waits for a new state"
            )
            return

        @callback
        def _on_ev_report(_event: Event) -> None:
            self._async_recalc()

        self._state_change_unsubs.append(tracker(self.hass, entity_id, _on_ev_report))

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
        self.previous_mode = None
        await self.storage.async_save_previous_mode(None)
        self.persist_entry_options(self.hass, self.entry_id)
        self.set_mode(prev)
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
        """Re-evaluate control. Elapsed time alone does not send a command."""
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

    async def _async_run_calculator(self, *, opportunity: bool = True) -> None:
        """Compute the next battery command from current Home Assistant state."""
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
                    _create_issue(
                        hass,
                        const.DOMAIN,
                        f"{const.DOMAIN}_{self.entry_id}_sensor",
                        breaks_invisibly=False,
                        is_fixable=False,
                        severity=IssueSeverity.WARNING,
                        translation_key="sensor_unavailable",
                        translation_placeholders={"name": key},
                    )
                    self.set_mode(const.MODE_RELEASED)
                    self.async_update_listeners()

        now_wall = dt_util.utcnow()
        soc_fresh = self._telemetry_is_fresh(soc_st, now_wall, DEFAULT_SOC_FRESH_S)
        _mark_bad("grid", bool(grid_ok))
        _mark_bad("soc", soc_fresh)
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

        self._snapshot_grid_smoothed = grid_smoothed
        self._snapshot_batt_smoothed = batt_smoothed
        self._snapshot_cap_now = cap_now
        self._snapshot_latest_start = ls

        now_mono = time_mod.monotonic()
        now_wall = dt_util.utcnow()
        if self._writes_blocked():
            opportunity = False
        latest = ls if ls is not None else const.LATEST_START_NO_NEED
        settings = SessionSettings(
            mode=self._mode,
            soc=current_soc,
            soc_sensor_ok=bool(soc_ok),
            min_soc=self._min_soc,
            max_soc=self._max_soc,
            max_battery_power_w=self._max_battery_power,
            min_interval_s=self._send_interval_s,
            deadband_w=self._pd_deadband_w,
            reserve_target_soc=self._reserve_target_soc,
            boost_charge_power_w=self._boost_charge_power,
            manual_target_soc=self._manual_target_soc,
            manual_power_w=self._manual_power,
            reserve_protection_start=self._reserve_protection_time,
            peak_window_start=self._peak_window_time,
            capacity_tariff_enabled=self._capacity_tariff_enabled,
            cap_now=cap_now,
            effective_cap_w=effective_cap_threshold_w(
                self._max_desired_peak_w,
                monthly_peak,
                monthly_ok,
            ),
            latest_start_charge=latest,
            peak_window_deadline=deadline,
            now=now,
            soc_fresh=soc_fresh,
            exclude_ev=self.exclude_ev_effective,
        )
        plan = self._session.plan(
            settings,
            now_mono=now_mono,
            grid=self._grid_sample(poller, grid_st, now_mono, now_wall),
            battery=self._channel_sample(batt_st if batt_ok else None, now_mono, now_wall),
            ev=self._ev_sample(now_mono, now_wall),
            reported=self._reported_observation(now_mono, now_wall),
            opportunity=opportunity,
        )
        self.last_calc_output = CalculatorOutput(
            setpoint_w=plan.proposed_w,
            operating_state=plan.operating_state,
            reason_code=plan.reason_code,
        )
        self.target_setpoint = plan.proposed_w
        _LOGGER.debug(
            "pd: mode=%s proposed=%s send=%s state=%s reason=%s",
            self._mode,
            plan.proposed_w,
            plan.send_w,
            plan.operating_state,
            plan.reason_code,
        )
        self.async_update_listeners()
        if plan.send_w is None or plan.release or self._writes_blocked():
            return
        self._dispatch_plan(plan)

    def _grid_sample(
        self,
        poller: HomeWizardPoller | None,
        grid_state: State | None,
        now_mono: float,
        now_wall: datetime,
    ) -> ChannelSample:
        """Grid publication from the fast poller or a Home Assistant sensor."""
        if poller is not None:
            if poller.latest_w is None or poller.latest_mono is None:
                return ChannelSample(value_w=None, available=False)
            return ChannelSample(
                value_w=poller.latest_w,
                publication_id=f"hw:{poller.latest_mono:.9f}",
                source_mono=poller.latest_mono,
            )
        return self._channel_sample(grid_state, now_mono, now_wall)

    def _ev_sample(self, now_mono: float, now_wall: datetime) -> ChannelSample:
        """EV publication for this Home Assistant process.

        Grid and battery may fall back from last_reported to last_updated.
        EV power may not. Invalid telemetry is unavailable immediately and is
        not remembered from the previous reading.
        """
        entity_id = self.runtime.ev_power_entity_id
        if not entity_id:
            return ChannelSample(value_w=None, available=False)
        state = self.hass.states.get(entity_id)
        if state is None:
            return ChannelSample(value_w=None, available=False)
        attributes = getattr(state, "attributes", {}) or {}
        return ev_channel_sample(
            entity_id=entity_id,
            raw_state=None if state.state is None else str(state.state),
            unit=attributes.get("unit_of_measurement"),
            restored=bool(attributes.get(ATTR_RESTORED)),
            last_reported=getattr(state, "last_reported", None),
            ha_process_started_utc=self.ha_process_started_utc,
            now_mono=now_mono,
            now_wall=now_wall,
        )

    def _channel_sample(
        self,
        state: State | None,
        now_mono: float,
        now_wall: datetime,
    ) -> ChannelSample:
        """One sensor reading. Repeating the same state is not a new publication."""
        if state is None or state.state in ("unknown", "unavailable", None, ""):
            return ChannelSample(value_w=None, available=False)
        value = parse_optional_float_state(state)
        if value is None:
            return ChannelSample(value_w=None, available=False)
        stamp = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
        age: float | None = None
        publication_id: str | None = None
        if stamp is not None:
            try:
                age = max(0.0, (now_wall - stamp).total_seconds())
                publication_id = f"{state.entity_id}:{stamp.timestamp()}"
            except (TypeError, AttributeError, ValueError):
                age = None
                publication_id = None
        return ChannelSample(
            value_w=value,
            publication_id=publication_id,
            source_mono=now_mono,
            wall_age_s=age,
        )

    def _telemetry_is_fresh(self, state: Any, now_wall: datetime, limit_s: float) -> bool:
        """True when a state string is present and its publication is still inside the limit."""
        if state is None or state.state in ("unknown", "unavailable", None, ""):
            return False
        _publication_id, observed, fresh = self._state_observation(
            state,
            time_mod.monotonic(),
            now_wall,
            limit_s,
        )
        return fresh and observed is not None

    def _state_observation(
        self,
        state: Any,
        now_mono: float,
        now_wall: datetime,
        limit_s: float,
    ) -> tuple[str | None, float | None, bool]:
        """Publication id, monotonic observation time, and freshness for one entity state."""
        if state is None or state.state in ("unknown", "unavailable", None, ""):
            return None, None, False
        stamp = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
        if stamp is None:
            return None, None, False
        try:
            age = max(0.0, (now_wall - stamp).total_seconds())
            publication_id = f"{state.entity_id}:{stamp.timestamp()}"
        except (TypeError, AttributeError, OSError, ValueError):
            return None, None, False
        return publication_id, now_mono - age, age <= limit_s

    def _reported_observation(self, now_mono: float, now_wall: datetime) -> ReportedObservation:
        """Build a publication-aware battery command readback.

        An active charge or discharge mode with a missing setpoint is incoherent.
        That observation stays unknown and is not published as 0 W.
        """
        resolved = self.runtime.resolved
        mode_state = self.hass.states.get(resolved.force_mode)
        charge_state = self.hass.states.get(resolved.set_charge_power)
        discharge_state = self.hass.states.get(resolved.set_discharge_power)
        mode_name = None
        if mode_state is not None and mode_state.state not in (
            "unknown",
            "unavailable",
            None,
            "",
        ):
            mode_name = str(mode_state.state)
        charge = parse_optional_float_state(charge_state)
        discharge = parse_optional_float_state(discharge_state)
        command = reported_command_w(mode_name, charge, discharge) if mode_name else None
        lowered = (mode_name or "").strip().lower()
        if lowered in {"stop", "standby", "neutral", "off", "idle"}:
            parts = [mode_state]
            coherent = command == 0
        elif lowered == const.FORCE_CHARGE:
            parts = [mode_state, charge_state]
            coherent = command is not None
        elif lowered == const.FORCE_DISCHARGE:
            parts = [mode_state, discharge_state]
            coherent = command is not None
        else:
            return ReportedObservation(
                command_w=None,
                publication_id=None,
                observed_mono=None,
                fresh=False,
                coherent=False,
            )
        ids: list[str] = []
        monos: list[float] = []
        fresh = True
        for part in parts:
            publication_id, observed, part_fresh = self._state_observation(
                part,
                now_mono,
                now_wall,
                DEFAULT_BATTERY_FRESH_S,
            )
            if publication_id is None or observed is None or not part_fresh:
                fresh = False
                continue
            ids.append(publication_id)
            monos.append(observed)
        if not coherent or not fresh or not ids:
            return ReportedObservation(
                command_w=command if coherent else None,
                publication_id=None,
                observed_mono=None,
                fresh=False,
                coherent=coherent and fresh,
            )
        return ReportedObservation(
            command_w=command,
            publication_id="|".join(ids),
            observed_mono=min(monos),
            fresh=True,
            coherent=True,
        )

    def _reported_battery_command(self) -> int | None:
        """Signed watts reported by the Marstek force mode and power numbers."""
        resolved = self.runtime.resolved
        mode_state = self.hass.states.get(resolved.force_mode)
        if mode_state is None or mode_state.state in ("unknown", "unavailable", None, ""):
            return None
        charge = parse_optional_float_state(self.hass.states.get(resolved.set_charge_power))
        discharge = parse_optional_float_state(
            self.hass.states.get(resolved.set_discharge_power)
        )
        return reported_command_w(mode_state.state, charge, discharge)

    def _writes_blocked(self) -> bool:
        """True while restart grace or the Modbus breaker forbids a write."""
        if self._write_grace_until is not None:
            if dt_util.utcnow() >= self._write_grace_until:
                _LOGGER.info("Restart write grace elapsed; Modbus writes enabled")
                self._write_grace_until = None
            else:
                return True
        return self._modbus_breaker_blocks()

    def _dispatch_plan(self, plan: ControlPlan) -> None:
        """Reserve one write synchronously, then start the task that submits it."""
        if (
            self._shutting_down
            or self._released_cleanup_active()
            or plan.send_w is None
            or self._writes_blocked()
        ):
            return
        spec = _Dispatch(
            plan.send_w,
            safety=plan.safety,
            reassert=plan.reassert,
            track_pd=plan.kind == "pd",
        )
        if self._active_dispatch is not None:
            if spec.command_w == self._active_dispatch.command_w:
                self._queued_dispatch = None
            else:
                self._queued_dispatch = spec
            return
        if self._session.explicit_stale_zero:
            self._session.note_stale_soc_zero_attempted()
            self._session.explicit_stale_zero = False
        self._active_dispatch = spec
        self._queued_dispatch = None
        self._command_task = self.hass.async_create_task(
            self._async_write_command(),
            eager_start=False,
        )

    async def _async_write_command(self) -> None:
        """Send the reserved command, then at most the newest command queued behind it."""
        try:
            while self._active_dispatch is not None:
                if self._shutting_down or self._session.release_requested:
                    self._queued_dispatch = None
                    self._active_dispatch = None
                    return
                spec = self._active_dispatch
                pd_state = self._session.pd if spec.track_pd else None
                try:
                    result = await asyncio.wait_for(
                        deliver_command(
                            _WriterTransport(self.writer),
                            self._session.ledger,
                            spec.command_w,
                            time_mod.monotonic(),
                            pd_state=pd_state,
                            safety=spec.safety,
                            reassert=spec.reassert,
                        ),
                        timeout=const.MODBUS_SEQUENCE_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    _LOGGER.warning(
                        "Modbus write timed out after %ss",
                        const.MODBUS_SEQUENCE_TIMEOUT_S,
                    )
                    self._record_modbus_sequence_failure(None)
                    self._drop_failed_dispatch(spec)
                    return
                except asyncio.CancelledError:
                    raise
                if result.accepted and result.sent_w is not None:
                    self.last_sent_setpoint = result.sent_w
                    self._record_modbus_sequence_success()
                    _LOGGER.debug("Modbus write OK: setpoint=%s W", result.sent_w)
                elif result.queued:
                    _LOGGER.debug(
                        "Reserved command %s was already in flight",
                        spec.command_w,
                    )
                elif (
                    not result.uncertain
                    and spec.command_w == self._session.ledger.accepted_w
                    and not spec.safety
                    and not spec.reassert
                ):
                    pass
                else:
                    self._record_modbus_sequence_failure(None)
                    if spec.safety and spec.command_w == 0:
                        self._session.note_stale_soc_zero_failed()
                    if self._modbus_breaker_blocks():
                        self._drop_failed_dispatch(spec)
                        return
                if self._shutting_down or self._session.release_requested:
                    self._queued_dispatch = None
                    self._active_dispatch = None
                    return
                nxt = self._queued_dispatch
                self._queued_dispatch = None
                if nxt is None or (
                    nxt.command_w == spec.command_w and not nxt.safety and not nxt.reassert
                ):
                    self._active_dispatch = None
                    return
                if nxt.safety and nxt.command_w == 0:
                    self._session.note_stale_soc_zero_attempted()
                self._active_dispatch = nxt
        finally:
            self._command_task = None
            self.async_update_listeners()

    def _drop_failed_dispatch(self, spec: _Dispatch) -> None:
        """Forget queued work after a failure. A fresh calculation replaces it."""
        if spec.safety and spec.command_w == 0:
            self._session.note_stale_soc_zero_failed()
        self._active_dispatch = None
        self._queued_dispatch = None

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
        _create_issue(
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
        """Open a control opportunity. A quiet interval does not write."""
        if self._mode == const.MODE_RELEASED:
            return
        await self._async_run_calculator(opportunity=True)

    async def async_schedule_released_cleanup(self) -> None:
        """Schedule Released cleanup once. A running sequence is left in place."""
        self._schedule_released_cleanup()

    async def _async_released_cleanup_when_able(self) -> None:
        """Run one Released cleanup. A failure arms a single later retry."""
        try:
            self._session.prepare_release()
            self._queued_dispatch = None
            command_task = self._command_task
            if command_task is not None and not command_task.done():
                try:
                    await command_task
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.debug("In-flight command ended during release", exc_info=True)
            try:
                if self._shutting_down:
                    return
                if self._mode_after_cleanup is not None or self._mode != const.MODE_RELEASED:
                    self._finish_cleanup_attempt(success=True)
                    return
                if self._cleanup_delay_s() > 0:
                    self._arm_released_cleanup()
                    return
                _LOGGER.info("Running Released cleanup sequence")
                self._cleanup_physical = True
                try:
                    await asyncio.wait_for(
                        self.writer.async_cleanup_released_sequence(),
                        timeout=const.MODBUS_SEQUENCE_TIMEOUT_S,
                    )
                finally:
                    self._cleanup_physical = False
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Released cleanup timed out after %ss",
                    const.MODBUS_SEQUENCE_TIMEOUT_S,
                )
                self._record_modbus_sequence_failure(None)
                self._session.note_release_finished(accepted=False)
                self.last_sent_setpoint = None
                self.async_update_listeners()
                self._finish_cleanup_attempt(success=False)
                return
            except Exception as err:
                _LOGGER.warning("Released cleanup failed: %s", err)
                self._record_modbus_sequence_failure(err)
                self._session.note_release_finished(accepted=False)
                self.last_sent_setpoint = None
                self.async_update_listeners()
                self._finish_cleanup_attempt(success=False)
                return

            self._record_modbus_sequence_success()
            self._session.note_release_finished(accepted=True)
            self._inherited_released_uncertainty = False
            self.last_sent_setpoint = None
            self.async_update_listeners()
            self._finish_cleanup_attempt(success=True)
        finally:
            current = asyncio.current_task()
            if self._released_cleanup_task is current:
                self._released_cleanup_task = None
            self._cleanup_physical = False

    def _arm_released_retry(self) -> None:
        """Arm one cleanup retry after the failed attempt has left the wire."""
        if (
            self._shutting_down
            or self._mode != const.MODE_RELEASED
            or self._mode_after_cleanup is not None
            or self._released_cleanup_unsub is not None
        ):
            return
        self._release_cleanup_owned = True
        delay = self._cleanup_delay_s()
        self._released_cleanup_unsub = async_call_later(
            self.hass,
            max(0.0, delay),
            self._on_released_cleanup_due,
        )

    def _finish_cleanup_attempt(self, *, success: bool) -> None:
        """Apply a mode chosen during cleanup, or retry a failed Released attempt."""
        pending = self._mode_after_cleanup
        self._mode_after_cleanup = None
        self._cleanup_physical = False
        if pending is not None or self._mode != const.MODE_RELEASED:
            self._release_cleanup_owned = False
            self._inherited_released_uncertainty = False
            self._cancel_released_wait()
            self._session.release_requested = False
            if pending is not None:
                self._mode = pending
            self._release_cleanup_exclusivity()
            self._async_recalc()
            return
        if success or self._shutting_down:
            return
        self._arm_released_retry()

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
