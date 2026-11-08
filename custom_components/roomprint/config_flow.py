"""Config flow for Roomprint — one tracked Bluetooth device, single instance."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .const import DOMAIN

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def _normalize_address(address: str) -> str | None:
    """Validate and normalize a colon-separated MAC (case-insensitive)."""
    address = address.strip()
    if not _MAC_RE.match(address):
        return None
    return address.upper()


class RoomprintConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Roomprint config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: Any,
    ) -> OptionsFlow:
        """Return the options flow (Configure in the UI)."""
        return RoomprintOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Single step: ask for the tracked device's Bluetooth address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = _normalize_address(user_input.get(CONF_ADDRESS, ""))
            if address is None:
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Roomprint",
                    data={CONF_ADDRESS: address},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
            errors=errors,
        )


class RoomprintOptionsFlow(OptionsFlow):
    """Change the tracked device's Bluetooth address after setup.

    Reached from Settings → Devices & services → Roomprint → Configure.
    Same validation and uppercasing as the setup flow. Saving reloads
    the entry, so the sensor unsubscribes from the old address and
    subscribes to the new one.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Single step: the Bluetooth address, pre-filled with the current one."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = _normalize_address(user_input.get(CONF_ADDRESS, ""))
            if address is None:
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                return self.async_create_entry(
                    title="",
                    data={CONF_ADDRESS: address},
                )
        current = (
            self.config_entry.options.get(CONF_ADDRESS)
            or self.config_entry.data[CONF_ADDRESS]
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS, default=current): str}
            ),
            errors=errors,
        )
