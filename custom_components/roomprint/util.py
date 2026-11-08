"""Shared helpers — scanner keying used by both the sensor and the survey."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

# RSSI sentinel for "unknown" (used by the Bluetooth stack).
RSSI_UNKNOWN = -127


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
