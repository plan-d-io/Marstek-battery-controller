"""Tests for Marstek Modbus setup description helpers."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.marstek_battery_controller import const
from custom_components.marstek_battery_controller.marstek_setup_i18n import (
    _MARSTEK_SETUP_TRANS_PREFIX,
    build_marstek_setup_description_placeholders,
)


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


def _nl_translations() -> dict[str, str]:
    prefix = _MARSTEK_SETUP_TRANS_PREFIX
    return {
        prefix + "description_enable_one": (
            "1 vereiste bedieningsentiteit is standaard uitgeschakeld ({roles})"
        ),
        prefix + "description_enable_many": (
            "{count} vereiste bedieningsentiteiten zijn standaard uitgeschakeld ({roles})"
        ),
        prefix + "description_interval": (
            "Polling met hoge prioriteit staat op {current} s; we raden {recommended} s aan"
        ),
        prefix + "description_reload": (
            " Het toepassen van de geselecteerde wijzigingen herlaadt Marstek Modbus één keer"
        ),
        prefix + "description_user_disabled": (
            " Sommige entiteiten zijn handmatig uitgeschakeld via Instellingen: {roles}"
        ),
    }


def test_marstek_setup_description_interval_only_uses_translations() -> None:
    plan = _plan_stub(current_high=10)
    ph = build_marstek_setup_description_placeholders(plan, _nl_translations())
    assert "Polling met hoge prioriteit" in ph["setup_details"]
    assert "10" in ph["setup_details"]
    assert "2" in ph["setup_details"]
    assert ph["user_disabled_note"] == ""


def test_marstek_setup_description_enable_and_user_note_dutch() -> None:
    plan = _plan_stub(
        needs_enable=True,
        integration_disabled_roles=(const.ROLE_FORCE_MODE,),
        user_disabled_roles=(const.ROLE_RS485_CONTROL,),
    )
    ph = build_marstek_setup_description_placeholders(plan, _nl_translations())
    assert const.ROLE_FORCE_MODE in ph["setup_details"]
    assert "vereiste bedieningsentiteit" in ph["setup_details"]
    assert const.ROLE_RS485_CONTROL in ph["user_disabled_note"]
    assert "Instellingen" in ph["user_disabled_note"]
