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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EVENT_LIVE, EVENT_ROOM_CHANGED, LIVE_EVENT_MIN_INTERVAL
from .util import (
    RSSI_UNKNOWN,
    resolved_address,
    resolved_label,
    resolved_person,
    scanner_key,
)

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

    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        # Options (Configure) can change the address after setup.
        self._address = resolved_address(entry)
        # The entity is named after the device's label, so each person's
        # sensor reads like "Alex's iPhone room". The existing entry has
        # no label and falls back to its title, "Roomprint", so its name
        # stays "Roomprint room" and its entity id does not change.
        self._label = resolved_label(entry)
        self._attr_name = f"{self._label} room"
        # The linked native Person entity (or None). This — never the
        # typed label — is what the room events report as the person.
        # The entry reloads on options change, so this stays fresh.
        self._person = resolved_person(entry)
        # Unique id is the config entry id — stable across address
        # changes, so the entity survives a re-address. Never key it on
        # the address itself.
        self._attr_unique_id = f"{entry.entry_id}-room"
        self._attr_native_value = "unknown"
        self._attr_extra_state_attributes: dict[str, Any] = {
            "confidence": None,
            "margin": None,
            "scores": None,
            "person": self._person,
        }
        # scanner_key -> (rssi, timestamp of last hearing)
        self._latest: dict[str, tuple[float, float]] = {}
        self._last_event_at = 0.0
        self._last_room_id: str | None = None
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
        # The simulator drives the room through this same method; the
        # websocket API reaches it via the runtime dict.
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        runtime["apply_live"] = self.apply_live
        self.async_on_remove(lambda: runtime.pop("apply_live", None))

    @callback
    def apply_live(
        self, live: dict[str, float | None], now: float
    ) -> dict[str, Any] | None:
        """Evaluate one live vector, update state, fire events.

        Single entry point for both the Bluetooth callback and the
        simulator: evaluate, update the sensor, fire a throttled
        roomprint_live event, and fire roomprint_room_changed whenever
        the estimated room actually changes. Returns the matcher result,
        or None when there is nothing to match against yet.
        """
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        matcher = runtime.get("matcher")
        if matcher is None or not matcher.fingerprints:
            return None
        result = matcher.evaluate(live, now)
        room_id = result["room"]
        # The device tracker reads the current room from here.
        runtime["current_room"] = room_id
        if room_id is not None and room_id != self._last_room_id:
            self.hass.bus.async_fire(
                EVENT_ROOM_CHANGED,
                {
                    "from_room": self._last_room_id,
                    "to_room": room_id,
                    "confidence": result["confidence"],
                    "device": self._address,
                    "label": self._label,
                    "person": self._person,
                    "entity_id": self.entity_id,
                },
            )
            self._last_room_id = room_id
        self._update_state(result)
        if now - self._last_event_at >= LIVE_EVENT_MIN_INTERVAL:
            self._last_event_at = now
            self.hass.bus.async_fire(
                EVENT_LIVE,
                {
                    "readings": {
                        key: value for key, value in live.items() if value is not None
                    },
                    "room": room_id,
                    "confidence": result["confidence"],
                    "device": self._address,
                    "label": self._label,
                    "person": self._person,
                    "entity_id": self.entity_id,
                },
            )
        return result

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
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        if runtime.get("simulation"):
            return  # never mix real advertisements with a simulation
        now = time.time()
        # Real advertisements only — the simulator path (apply_live)
        # never touches last_seen, so the tracker can't be kept home
        # by fake readings.
        runtime["last_seen"] = now
        self._latest[scanner_key(self.hass, service_info.source)] = (rssi, now)
        live = {
            key: rssi_value
            for key, (rssi_value, heard_at) in self._latest.items()
            if now - heard_at <= LIVE_VECTOR_MAX_AGE
        }
        self.apply_live(live, now)

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
