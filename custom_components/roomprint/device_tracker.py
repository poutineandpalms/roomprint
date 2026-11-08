"""Device tracker platform — home/not_home for the tracked device.

Feeds Home Assistant's native Person: link this tracker to a person in
Settings → People and the person shows home while the phone is heard,
not_home once it goes quiet. The room itself is NOT the tracker's
state — rooms live on the room sensor (and as this tracker's ``area``
attribute), following Bermuda's proven shape: a scanner entity whose
state comes from ``is_connected`` alone.

The tracker never hears Bluetooth itself. It reads two values the
sensor maintains in the per-entry runtime: ``last_seen`` (timestamp of
the last accepted real advertisement) and ``current_room`` (latest
matcher result). The simulator never touches ``last_seen``, so fake
readings cannot keep the tracker home.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from homeassistant.components.device_tracker import BaseScannerEntity
from homeassistant.components.device_tracker.const import SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .util import resolved_label

# The phone counts as home while heard within this long; after that it
# is not_home. (Bermuda uses a similar consider-home window.)
CONSIDER_HOME_SECONDS = 180.0
# Nothing re-evaluates the tracker on its own when the phone goes quiet,
# so check on this interval and write state when anything changed.
CHECK_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Roomprint device tracker."""
    async_add_entities([RoomprintDeviceTracker(hass, entry)])


class RoomprintDeviceTracker(BaseScannerEntity):
    """Home/not_home for one tracked device, with its room as an attribute."""

    _attr_should_poll = False
    _attr_source_type = SourceType.BLUETOOTH_LE

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        # Named after the device's label, like the room sensor. The
        # existing entry falls back to its title, "Roomprint", so its
        # name stays "Roomprint Tracker" and its entity id does not
        # change — a link to a Person survives.
        self._attr_name = f"{resolved_label(entry)} tracker"
        # Unique id is the config entry id — stable across address
        # changes, so the tracker (and its link to a Person) survives
        # a re-address. Never key it on the address itself.
        self._attr_unique_id = f"{entry.entry_id}-tracker"
        self._last_seen_state: tuple[bool | None, str | None] | None = None

    def _runtime(self) -> dict[str, Any]:
        """Per-entry runtime data shared with the sensor."""
        return self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})

    @property
    def is_connected(self) -> bool | None:
        """True while the phone was heard recently, False when stale.

        None when never seen: the tracker's state is unknown, not home
        and not not_home.
        """
        last_seen = self._runtime().get("last_seen")
        if last_seen is None:
            return None
        return (time.time() - last_seen) <= CONSIDER_HOME_SECONDS

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The room as an attribute (state stays home/not_home/unknown)."""
        room_id = self._runtime().get("current_room")
        area_name = None
        if room_id is not None:
            area = ar.async_get(self.hass).async_get_area(room_id)
            area_name = area.name if area is not None else room_id
        return {"area": area_name, "area_id": room_id}

    async def async_added_to_hass(self) -> None:
        """Check connectedness (and the room attribute) on an interval."""
        self._last_seen_state = (self.is_connected, self._runtime().get("current_room"))
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_check_connected, CHECK_INTERVAL
            )
        )

    @callback
    def _async_check_connected(self, now: Any) -> None:
        """Write state when connectedness or the room changed."""
        state = (self.is_connected, self._runtime().get("current_room"))
        if state != self._last_seen_state:
            self._last_seen_state = state
            self.async_write_ha_state()
