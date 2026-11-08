"""Constants for the Roomprint integration."""

from __future__ import annotations

DOMAIN = "roomprint"
STORAGE_KEY = "roomprint"
STORAGE_VERSION = 1

PLATFORMS: list[str] = ["sensor"]

# Event fired on the bus (throttled) so the survey panel can show live bars.
EVENT_LIVE = "roomprint_live"
LIVE_EVENT_MIN_INTERVAL = 0.5  # seconds between roomprint_live events

# Event fired on the bus whenever the estimated room actually changes.
EVENT_ROOM_CHANGED = "roomprint_room_changed"
