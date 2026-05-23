"""Tests for Marstek Modbus setup description helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.marstek_battery_controller import const

pytest.importorskip("homeassistant")


def _plan_stub(
    *,
    needs_enable: bool = False,
    needs_interval: bool = True,
    integration_disabled_roles: tuple[str, ...] = (),
    user_disabled_roles: tuple[str, ...] = (),
    current_high: int = 10,
) -> SimpleNamespace:
    return SimpleNamespace(
        needs_enable=needs_enable,
        needs_interval=needs_interval,
        integration_disabled_roles=integration_disabled_roles,
        user_disabled_roles=user_disabled_roles,
        current_high_interval=current_high,
    )


def test_marstek_setup_description_interval_only() -> None:
    from custom_components.marstek_battery_controller.config_flow import (
        _marstek_setup_description_placeholders,
    )

    plan = _plan_stub(current_high=10)
    ph = _marstek_setup_description_placeholders(plan)
    assert "10s" in ph["setup_details"]
    assert "2s" in ph["setup_details"]
    assert ph["user_disabled_note"] == ""


def test_marstek_setup_description_enable_and_user_note() -> None:
    from custom_components.marstek_battery_controller.config_flow import (
        _marstek_setup_description_placeholders,
    )

    plan = _plan_stub(
        needs_enable=True,
        integration_disabled_roles=(const.ROLE_FORCE_MODE,),
        user_disabled_roles=(const.ROLE_RS485_CONTROL,),
    )
    ph = _marstek_setup_description_placeholders(plan)
    assert const.ROLE_FORCE_MODE in ph["setup_details"]
    assert const.ROLE_RS485_CONTROL in ph["user_disabled_note"]
