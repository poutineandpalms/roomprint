"""Shared helpers — scanner keying used by both the sensor and the survey."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_PERSON

# RSSI sentinel for "unknown" (used by the Bluetooth stack).
RSSI_UNKNOWN = -127


def resolved_address(entry: ConfigEntry) -> str:
    """The tracked Bluetooth address for an entry.

    The options flow (Configure) can change the address after setup, so
    the option wins; entries created before options existed only have
    the setup data. One definition, used by setup, sensor, and tracker.
    """
    options: dict[str, Any] = entry.options or {}
    return options.get(CONF_ADDRESS) or entry.data[CONF_ADDRESS]


def resolved_label(entry: ConfigEntry) -> str:
    """The display label for an entry's device.

    The options flow (Configure) can change the label after setup, so
    the option wins; then the setup data; then the entry title. The
    existing entry (created before labels existed) has no label stored,
    so it falls back to its title, "Roomprint" — its sensor keeps the
    name "Roomprint room".
    """
    options: dict[str, Any] = entry.options or {}
    return options.get(CONF_NAME) or entry.data.get(CONF_NAME) or entry.title


def resolved_person(entry: ConfigEntry) -> str | None:
    """The linked person entity_id for an entry, or None.

    The person is picked from Home Assistant's native person entities
    in setup/Configure — never the typed label, which is only the
    device's display name. Options win over setup data; entries created
    before the picker existed have no person. Unlike the address and
    label, None is a legitimate value here (the person was cleared),
    so presence of the key — not truthiness — decides which store wins.
    """
    options: dict[str, Any] = entry.options or {}
    if CONF_PERSON in options:
        return options[CONF_PERSON] or None
    return entry.data.get(CONF_PERSON) or None


def scanner_key(hass: HomeAssistant, source: str) -> str:
    """Key for the scanner that heard an advertisement.

    The scanner's Home Assistant area when it has one (that is what the
    room list and the fingerprints are keyed by), otherwise the scanner's
    own source address so the data is still usable.
    """
    registry = dr.async_get(hass)
    device = registry.async_get_device(
        set(), {(dr.CONNECTION_BLUETOOTH, source)}
    ) or registry.async_get_device(
        set(), {(dr.CONNECTION_NETWORK_MAC, source)}
    )
    if device is not None and device.area_id is not None:
        return device.area_id
    return source
