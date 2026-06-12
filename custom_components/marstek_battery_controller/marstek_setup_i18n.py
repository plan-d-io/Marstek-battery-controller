"""Translated strings for the Marstek Modbus config-flow setup step."""

from __future__ import annotations

from typing import Any, Protocol

from . import const

_MARSTEK_SETUP_TRANS_PREFIX = (
    f"component.{const.DOMAIN}.config.step.marstek_modbus_setup."
)
_MARSTEK_SETUP_TRANS_FALLBACKS: dict[str, str] = {
    "description_enable_one": (
        "1 required control entity is disabled by default ({roles})"
    ),
    "description_enable_many": (
        "{count} required control entities are disabled by default ({roles})"
    ),
    "description_interval": (
        "High-priority polling is {current}s; we recommend {recommended}s"
    ),
    "description_reload": (
        " Applying selected changes reloads Marstek Modbus once "
        "and may take 1–2 minutes"
    ),
    "description_user_disabled": (
        " Some entities were disabled manually and must be re-enabled "
        "in Settings: {roles}"
    ),
}


class MarstekSetupPlanLike(Protocol):
    """Minimal setup plan surface for description placeholders."""

    needs_enable: bool
    needs_interval: bool
    integration_disabled_roles: tuple[str, ...]
    user_disabled_roles: tuple[str, ...]
    current_high_interval: int


def build_marstek_setup_description_placeholders(
    plan: MarstekSetupPlanLike,
    translations: dict[str, str],
) -> dict[str, str]:
    """Build config step description placeholders using loaded translations."""

    def t(suffix: str, **kwargs: Any) -> str:
        key = _MARSTEK_SETUP_TRANS_PREFIX + suffix
        template = translations.get(key, _MARSTEK_SETUP_TRANS_FALLBACKS[suffix])
        return template.format(**kwargs)

    details: list[str] = []
    if plan.needs_enable:
        roles = ", ".join(plan.integration_disabled_roles)
        count = len(plan.integration_disabled_roles)
        if count == 1:
            details.append(t("description_enable_one", roles=roles))
        else:
            details.append(t("description_enable_many", count=count, roles=roles))
    if plan.needs_interval:
        details.append(
            t(
                "description_interval",
                current=plan.current_high_interval,
                recommended=const.VIPER_HIGH_INTERVAL_RECOMMENDED,
            )
        )

    user_note = ""
    if plan.user_disabled_roles:
        user_note = t(
            "description_user_disabled",
            roles=", ".join(plan.user_disabled_roles),
        )

    return {
        "setup_details": ". ".join(details),
        "reload_note": t("description_reload"),
        "user_disabled_note": user_note,
        "current": str(plan.current_high_interval),
        "recommended": str(const.VIPER_HIGH_INTERVAL_RECOMMENDED),
    }
