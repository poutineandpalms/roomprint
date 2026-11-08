"""The Roomprint integration — Bluetooth room fingerprints, all local."""

from __future__ import annotations

import os
from typing import Any

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, PLATFORMS, STORAGE_KEY, STORAGE_VERSION
from .matcher import RoomMatcher
from .util import resolved_address
from .websocket import async_setup as async_setup_websocket

PANEL_ICON = "mdi:bluetooth"
PANEL_TITLE = "Room Survey"


def _runtime(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Per-entry runtime data shared by the sensor platform and websocket API."""
    return hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Roomprint from a config entry."""
    runtime = _runtime(hass, entry)
    # Options (Configure) can change the address after setup; the option
    # wins, the setup data is the fallback.
    runtime["address"] = resolved_address(entry)

    # Live state shared by the sensor and the device tracker. current_room
    # is the latest matcher result (area_id or None); last_seen is the
    # timestamp of the last accepted real advertisement (never set by
    # the simulator).
    runtime["current_room"] = None
    runtime["last_seen"] = None

    # Fingerprints live in Home Assistant storage — nothing leaves the box.
    store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    runtime["store"] = store
    stored = await store.async_load() or {}
    fingerprints: dict[str, Any] = stored.get("fingerprints", {})
    runtime["fingerprints"] = fingerprints
    runtime["matcher"] = RoomMatcher(fingerprints)

    # Survey state (owned by websocket.py).
    runtime["survey"] = {
        "recording": False,
        "area_id": None,
        "started_at": None,
        "samples": [],
        "unsub": None,
    }

    # Simulation state (owned by websocket.py). Sim fingerprints live in
    # memory ONLY — they are never written to the storage Store, so they
    # can never pollute real survey data.
    runtime["sim_fingerprints"] = {}
    runtime["sim_geometries"] = {}
    runtime["sim_rng"] = None
    runtime["simulation"] = False

    async_setup_websocket(hass)
    await _async_register_panel(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Reload the entry when the options change (e.g. a new address from
    # Configure), so the sensor unsubscribes from the old address and
    # subscribes to the new one.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry after an options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    survey = runtime.get("survey", {})
    if survey.get("recording") and survey.get("unsub") is not None:
        survey["unsub"]()
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Register the Room Survey sidebar panel (once per Home Assistant run)."""
    if hass.data.get(DOMAIN, {}).get("panel_registered"):
        return
    hass.data.setdefault(DOMAIN, {})["panel_registered"] = True

    frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
    panel_url = f"/{DOMAIN}_panel"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(panel_url, frontend_dir, cache_headers=False)]
    )
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="roomprint-panel",
        frontend_url_path=DOMAIN,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=f"{panel_url}/roomprint-panel.js",
        embed_iframe=False,
        require_admin=True,
    )
