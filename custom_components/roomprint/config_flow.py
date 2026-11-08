"""Config flow for Roomprint — one entry per tracked Bluetooth device.

A household adds one entry per phone: each entry is named (its label),
tracks one Bluetooth address, and optionally links the device to one
of Home Assistant's native Person entities. The person is what the
room events report — the typed label is only the device's display
name and never identifies the person. The same address cannot be
tracked twice — the entry's unique id is the address, and setup/options
check the resolved address of every existing entry before accepting one.
"""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import CONF_PERSON, DOMAIN
from .util import resolved_address, resolved_label, resolved_person

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def _normalize_address(address: str) -> str | None:
    """Validate and normalize a colon-separated MAC (case-insensitive)."""
    address = address.strip()
    if not _MAC_RE.match(address):
        return None
    return address.upper()


def _address_in_use(
    hass: HomeAssistant, address: str, exclude_entry_id: str | None = None
) -> bool:
    """True when any Roomprint entry already tracks this address.

    Checks each entry's resolved address (options first, data fallback),
    so it catches entries created before labels/options existed too.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if exclude_entry_id is not None and entry.entry_id == exclude_entry_id:
            continue
        if resolved_address(entry) == address:
            return True
    return False


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
        """Single step: a name, a Bluetooth address, an optional person."""
        errors: dict[str, str] = {}
        if user_input is not None:
            label = (user_input.get(CONF_NAME) or "").strip()
            address = _normalize_address(user_input.get(CONF_ADDRESS, ""))
            person = (user_input.get(CONF_PERSON) or "").strip() or None
            if not label:
                errors[CONF_NAME] = "invalid_label"
            elif address is None:
                errors[CONF_ADDRESS] = "invalid_address"
            elif _address_in_use(self.hass, address):
                errors[CONF_ADDRESS] = "address_in_use"
            else:
                # The entry's unique id is the address: the same phone
                # cannot be added twice. The explicit check above is the
                # one that reports the error; this is the backstop.
                await self.async_set_unique_id(address)
                return self.async_create_entry(
                    title=label,
                    data={
                        CONF_ADDRESS: address,
                        CONF_NAME: label,
                        CONF_PERSON: person,
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): str,
                    vol.Required(CONF_ADDRESS): str,
                    vol.Optional(CONF_PERSON): EntitySelector(
                        EntitySelectorConfig(domain="person")
                    ),
                }
            ),
            errors=errors,
        )


class RoomprintOptionsFlow(OptionsFlow):
    """Change the device's name, Bluetooth address, and person after setup.

    Reached from Settings → Devices & services → Roomprint → Configure.
    Same validation and uppercasing as the setup flow. Saving reloads
    the entry, so the sensor unsubscribes from the old address and
    subscribes to the new one. The duplicate-address guard skips this
    entry itself, so keeping the current address is always allowed.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Single step: name, Bluetooth address, and person, pre-filled."""
        errors: dict[str, str] = {}
        if user_input is not None:
            label = (user_input.get(CONF_NAME) or "").strip()
            address = _normalize_address(user_input.get(CONF_ADDRESS, ""))
            person = (user_input.get(CONF_PERSON) or "").strip() or None
            if not label:
                errors[CONF_NAME] = "invalid_label"
            elif address is None:
                errors[CONF_ADDRESS] = "invalid_address"
            elif _address_in_use(
                self.hass, address, exclude_entry_id=self.config_entry.entry_id
            ):
                errors[CONF_ADDRESS] = "address_in_use"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_ADDRESS: address,
                        CONF_NAME: label,
                        CONF_PERSON: person,
                    },
                )
        current_label = resolved_label(self.config_entry)
        current_address = resolved_address(self.config_entry)
        current_person = resolved_person(self.config_entry)
        # Only pre-fill the person selector when one is linked: a
        # default of None renders oddly, an absent default renders empty.
        person_field: Any = EntitySelector(EntitySelectorConfig(domain="person"))
        person_key = (
            vol.Optional(CONF_PERSON, default=current_person)
            if current_person
            else vol.Optional(CONF_PERSON)
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=current_label): str,
                    vol.Required(CONF_ADDRESS, default=current_address): str,
                    person_key: person_field,
                }
            ),
            errors=errors,
        )
