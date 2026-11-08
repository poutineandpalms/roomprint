"""Sensor platform — one sensor: which room the tracked device is in.

Subscribes to Home Assistant's Bluetooth advertisement stream (the same
feed Bermuda uses) and feeds per-scanner RSSI into the RoomMatcher.
Event-driven: no polling, and the callback only records a number and
returns — the matcher does a few dozen arithmetic ops per advertisement.
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EVENT_LIVE, LIVE_EVENT_MIN_INTERVAL
from .util import RSSI_UNKNOWN, scanner_key

# A scanner not heard from within this long drops out of the live vector,
# so a stale strong reading cannot pin the room after you walk away.
LIVE_VECTOR_MAX_AGE = 10.0  # seconds


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Roomprint room sensor."""
    async_add_entities([RoomprintRoomSensor(hass, entry)])


class RoomprintRoomSensor(SensorEntity):
    """Current room of the tracked device, as estimated by Roomprint."""

    _attr_name = "Roomprint room"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._address = entry.data[CONF_ADDRESS]
        self._attr_native_value = "unknown"
        self._attr_extra_state_attributes: dict[str, Any] = {
            "confidence": None,
            "margin": None,
            "scores": None,
        }
        # scanner_key -> (rssi, timestamp of last hearing)
        self._latest: dict[str, tuple[float, float]] = {}
        self._last_event_at = 0.0
        self._unsub: Any = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to the tracked device's advertisements."""
        self._unsub = bluetooth.async_register_callback(
            self.hass,
            self._async_handle_advert,
            bluetooth.BluetoothCallbackMatcher(
                address=self._address, connectable=False
            ),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        self.async_on_remove(self._unsub)

    @callback
    def _async_handle_advert(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Record one advertisement, re-evaluate the room, update state."""
        rssi = service_info.advertisement.rssi
        if rssi is None or rssi == RSSI_UNKNOWN:
            return
        now = time.time()
        self._latest[scanner_key(self.hass, service_info.source)] = (rssi, now)

        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        matcher = runtime.get("matcher")
        if matcher is None or not matcher.fingerprints:
            return
        live = {
            key: rssi_value
            for key, (rssi_value, heard_at) in self._latest.items()
            if now - heard_at <= LIVE_VECTOR_MAX_AGE
        }
        result = matcher.evaluate(live, now)
        self._update_state(result)
        if now - self._last_event_at >= LIVE_EVENT_MIN_INTERVAL:
            self._last_event_at = now
            self.hass.bus.async_fire(
                EVENT_LIVE,
                {
                    "readings": live,
                    "room": result["room"],
                    "confidence": result["confidence"],
                },
            )

    def _update_state(self, result: dict[str, Any]) -> None:
        """Apply a matcher result to the entity state and attributes."""
        room_id = result["room"]
        if room_id is None:
            self._attr_native_value = "unknown"
        else:
            area = ar.async_get(self.hass).async_get_area(room_id)
            self._attr_native_value = area.name if area is not None else room_id
        if result.get("margin") is not None:
            self._attr_extra_state_attributes["margin"] = result["margin"]
        if result.get("confidence") is not None:
            self._attr_extra_state_attributes["confidence"] = result["confidence"]
        if result.get("scores") is not None:
            self._attr_extra_state_attributes["scores"] = result["scores"]
        self.async_write_ha_state()
