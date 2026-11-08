"""WebSocket API for the Room Survey panel — admin only, all local.

Commands (all under the ``roomprint/`` namespace):
- roomprint/list_entries      -> [{entry_id, label, address,
                                   sensor_entity_id, tracker_entity_id}]
- roomprint/list_areas        -> [{area_id, name, surveyed}]
- roomprint/start_survey      {area_id, [entry_id]} -> {area_id, started_at}
- roomprint/stop_survey       {[entry_id]} -> {area_id, sample_count, fingerprint}
- roomprint/get_survey_status {[entry_id]} -> {recording, area_id, started_at, sample_count}
- roomprint/get_fingerprints  -> {area_id: fingerprint}
- roomprint/generate_sim_fingerprints {[entry_id]} -> {mapping, fingerprints}
- roomprint/set_simulation    {enabled, [entry_id]} -> {simulation}
- roomprint/sim_position      {x, y, [entry_id]} -> {readings, room, confidence}
- roomprint/get_simulation    {[entry_id]} -> {simulation, has_sim_fingerprints, geometries}

Commands that take an entry_id target that entry's device; when it is
omitted they use the first entry, so the single-device panel keeps
working unchanged. Fingerprints are shared across entries (one global
Store), but each entry has its own runtime: its own survey, its own
simulation, its own matcher.

The survey records the tracked device's advertisements with
``async_register_advertisement_callback`` (every packet, per scanner),
snapshotting the latest per-scanner RSSI at most twice a second so a
walk stays a few hundred samples, not tens of thousands.

The simulator (generate_sim_fingerprints / sim_position) drives the
same matcher and sensor through apply_live with dummy RSSI computed
from a floor map — no hardware needed. Sim fingerprints live in memory
only and are never written to storage.
"""

from __future__ import annotations

import random
import time
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth, websocket_api
from homeassistant.components.websocket_api import ActiveConnection
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .matcher import RoomMatcher, compute_fingerprint
from .simulate import DEFAULT_MAP, generate_fingerprints, readings_for_point
from .util import RSSI_UNKNOWN, resolved_address, resolved_label, scanner_key

# Snapshot the live readings into a survey sample at most this often.
SAMPLE_MIN_INTERVAL = 0.5  # seconds


def _get_runtime(
    hass: HomeAssistant, entry_id: str | None = None
) -> dict[str, Any] | None:
    """Runtime of one Roomprint config entry, or None if unset.

    The named entry when entry_id is given, the first entry when it is
    omitted. None when there are no entries, the id is unknown, or the
    entry is not loaded.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return None
    if entry_id is None:
        entry = entries[0]
    else:
        entry = next((e for e in entries if e.entry_id == entry_id), None)
        if entry is None:
            return None
    return hass.data.get(DOMAIN, {}).get(entry.entry_id)


def _resolve_runtime(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> dict[str, Any] | None:
    """Runtime for the entry a command targets, or send an error.

    The entry_id in the message wins; omitted means the first entry.
    Unknown ids get "unknown_entry", no entries (or an unloaded entry)
    get "not_configured". Returns None after sending the error.
    """
    entry_id = msg.get("entry_id")
    if entry_id is not None and not any(
        e.entry_id == entry_id
        for e in hass.config_entries.async_entries(DOMAIN)
    ):
        connection.send_error(
            msg["id"], "unknown_entry", f"No such entry: {entry_id}"
        )
        return None
    runtime = _get_runtime(hass, entry_id)
    if runtime is None:
        connection.send_error(msg["id"], "not_configured", "Roomprint is not set up.")
        return None
    return runtime


def _require_admin(
    connection: ActiveConnection, msg: dict[str, Any]
) -> bool:
    """Reject non-admin callers. Returns True when the caller is admin."""
    if connection.user is None or not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Admin only.")
        return False
    return True


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Register the roomprint/* websocket commands."""
    websocket_api.async_register_command(hass, websocket_list_entries)
    websocket_api.async_register_command(hass, websocket_list_areas)
    websocket_api.async_register_command(hass, websocket_start_survey)
    websocket_api.async_register_command(hass, websocket_stop_survey)
    websocket_api.async_register_command(hass, websocket_get_survey_status)
    websocket_api.async_register_command(hass, websocket_get_fingerprints)
    websocket_api.async_register_command(hass, websocket_generate_sim_fingerprints)
    websocket_api.async_register_command(hass, websocket_set_simulation)
    websocket_api.async_register_command(hass, websocket_sim_position)
    websocket_api.async_register_command(hass, websocket_get_simulation)


@websocket_api.websocket_command({vol.Required("type"): "roomprint/list_entries"})
@callback
def websocket_list_entries(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """List every Roomprint entry with its device and entity ids.

    The panel's device selector is built from this. Entity ids come
    from the entity registry via the entry-based unique ids, so they
    are null until the entry has loaded its platforms at least once.
    """
    if not _require_admin(connection, msg):
        return
    registry = er.async_get(hass)
    entries = [
        {
            "entry_id": entry.entry_id,
            "label": resolved_label(entry),
            "address": resolved_address(entry),
            "sensor_entity_id": registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}-room"
            ),
            "tracker_entity_id": registry.async_get_entity_id(
                "device_tracker", DOMAIN, f"{entry.entry_id}-tracker"
            ),
        }
        for entry in hass.config_entries.async_entries(DOMAIN)
    ]
    connection.send_result(msg["id"], entries)


@websocket_api.websocket_command({vol.Required("type"): "roomprint/list_areas"})
@callback
def websocket_list_areas(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """List areas with whether each one has a fingerprint yet."""
    if not _require_admin(connection, msg):
        return
    runtime = _get_runtime(hass)
    if runtime is None:
        connection.send_error(msg["id"], "not_configured", "Roomprint is not set up.")
        return
    surveyed = set(runtime.get("fingerprints", {}))
    areas = [
        {"area_id": area.id, "name": area.name, "surveyed": area.id in surveyed}
        for area in ar.async_get(hass).async_list_areas()
    ]
    connection.send_result(msg["id"], areas)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roomprint/start_survey",
        vol.Required("area_id"): str,
        vol.Optional("entry_id"): str,
    }
)
@callback
def websocket_start_survey(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Start recording a survey walk for one area."""
    if not _require_admin(connection, msg):
        return
    runtime = _resolve_runtime(hass, connection, msg)
    if runtime is None:
        return
    survey = runtime["survey"]
    if runtime.get("simulation"):
        connection.send_error(
            msg["id"],
            "simulation_on",
            "Turn simulation off before recording a real survey.",
        )
        return
    if survey["recording"]:
        connection.send_error(
            msg["id"], "already_recording", "A survey is already recording."
        )
        return
    area_id = msg["area_id"]
    if ar.async_get(hass).async_get_area(area_id) is None:
        connection.send_error(msg["id"], "unknown_area", f"No such area: {area_id}")
        return

    latest: dict[str, float] = {}
    last_snapshot = 0.0

    @callback
    def _collect(
        service_info: bluetooth.BluetoothServiceInfoBleak,
    ) -> None:
        # NOTE: the every-packet callback takes service_info only — unlike
        # async_register_callback, there is no `change` argument.
        nonlocal last_snapshot
        rssi = service_info.advertisement.rssi
        if rssi is None or rssi == RSSI_UNKNOWN:
            return
        latest[scanner_key(hass, service_info.source)] = rssi
        now = time.time()
        if now - last_snapshot >= SAMPLE_MIN_INTERVAL and latest:
            last_snapshot = now
            survey["samples"].append(
                {"timestamp": now, "readings": dict(latest)}
            )

    survey["recording"] = True
    survey["area_id"] = area_id
    survey["started_at"] = time.time()
    survey["samples"] = []
    survey["latest"] = latest
    survey["unsub"] = bluetooth.async_register_advertisement_callback(
        hass, _collect, runtime["address"]
    )
    connection.send_result(
        msg["id"], {"area_id": area_id, "started_at": survey["started_at"]}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roomprint/stop_survey",
        vol.Optional("entry_id"): str,
    }
)
@websocket_api.async_response
async def websocket_stop_survey(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Stop the survey, compute and save the fingerprint."""
    if not _require_admin(connection, msg):
        return
    runtime = _resolve_runtime(hass, connection, msg)
    if runtime is None:
        return
    survey = runtime["survey"]
    if not survey["recording"]:
        connection.send_error(msg["id"], "not_recording", "No survey is recording.")
        return
    if survey["unsub"] is not None:
        survey["unsub"]()
        survey["unsub"] = None
    survey["recording"] = False

    samples = survey["samples"]
    area_id = survey["area_id"]
    if not samples:
        connection.send_error(
            msg["id"], "no_samples", "No advertisements were recorded."
        )
        return
    try:
        fingerprint = compute_fingerprint(samples)
    except ValueError as err:
        connection.send_error(msg["id"], "bad_samples", str(err))
        return
    fingerprints = runtime["fingerprints"]
    fingerprints[area_id] = fingerprint
    if not runtime.get("simulation"):
        # While simulating, the live matcher runs dummy fingerprints;
        # the real ones are restored when simulation is turned off.
        runtime["matcher"].fingerprints = fingerprints
    await runtime["store"].async_save({"fingerprints": fingerprints})
    connection.send_result(
        msg["id"],
        {
            "area_id": area_id,
            "sample_count": len(samples),
            "fingerprint": fingerprint,
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roomprint/get_survey_status",
        vol.Optional("entry_id"): str,
    }
)
@callback
def websocket_get_survey_status(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Current survey state (for the panel timer and sample count)."""
    if not _require_admin(connection, msg):
        return
    runtime = _resolve_runtime(hass, connection, msg)
    if runtime is None:
        return
    survey = runtime["survey"]
    connection.send_result(
        msg["id"],
        {
            "recording": survey["recording"],
            "area_id": survey["area_id"],
            "started_at": survey["started_at"],
            "sample_count": len(survey["samples"]),
        },
    )


@websocket_api.websocket_command(
    {vol.Required("type"): "roomprint/get_fingerprints"}
)
@callback
def websocket_get_fingerprints(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """All stored fingerprints, keyed by area_id."""
    if not _require_admin(connection, msg):
        return
    runtime = _get_runtime(hass)
    if runtime is None:
        connection.send_error(msg["id"], "not_configured", "Roomprint is not set up.")
        return
    connection.send_result(msg["id"], dict(runtime.get("fingerprints", {})))


# ---------------- simulator ----------------

# Area-name keywords, in match order, onto the dummy-map room slugs.
_SIM_SLUGS = ("living", "kitchen", "master", "guest", "bath", "entrance")


def _slug_for_area(name: str) -> str | None:
    """Map a Home Assistant area name onto a dummy-map room slug."""
    lowered = name.lower()
    for slug in _SIM_SLUGS:
        if slug in lowered:
            return slug
    return None


def _apply_simulation(runtime: dict[str, Any], enabled: bool) -> None:
    """Swap the matcher's fingerprints between real and simulated.

    Replacing the matcher resets its current-room/dwell state, so the
    sensor never carries a stale room across the switch.
    """
    runtime["simulation"] = enabled
    if enabled:
        runtime["matcher"] = RoomMatcher(runtime.get("sim_fingerprints", {}))
    else:
        runtime["matcher"] = RoomMatcher(runtime.get("fingerprints", {}))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roomprint/generate_sim_fingerprints",
        vol.Optional("entry_id"): str,
    }
)
@callback
def websocket_generate_sim_fingerprints(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Build dummy fingerprints from the map and turn simulation on.

    Home Assistant areas are matched onto the dummy-map rooms by name.
    Sim fingerprints live in memory only — they are never saved.
    """
    if not _require_admin(connection, msg):
        return
    runtime = _resolve_runtime(hass, connection, msg)
    if runtime is None:
        return
    if runtime.get("survey", {}).get("recording"):
        connection.send_error(
            msg["id"], "recording", "Stop the survey before simulating."
        )
        return
    geometries: dict[str, dict] = {}
    mapping: dict[str, str] = {}
    for area in ar.async_get(hass).async_list_areas():
        slug = _slug_for_area(area.name)
        if slug is None:
            continue
        geometries[area.id] = DEFAULT_MAP[slug]
        mapping[area.id] = slug
    if not geometries:
        connection.send_error(
            msg["id"],
            "no_matching_areas",
            "No area name matched a dummy-map room (living, kitchen, "
            "master, guest, bath).",
        )
        return
    fingerprints = generate_fingerprints(geometries)
    runtime["sim_fingerprints"] = fingerprints
    runtime["sim_geometries"] = geometries
    runtime["sim_rng"] = random.Random()
    _apply_simulation(runtime, True)
    connection.send_result(
        msg["id"], {"mapping": mapping, "fingerprints": fingerprints}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roomprint/set_simulation",
        vol.Required("enabled"): bool,
        vol.Optional("entry_id"): str,
    }
)
@callback
def websocket_set_simulation(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Turn simulation on or off. On swaps in the dummy fingerprints;
    off restores the real ones."""
    if not _require_admin(connection, msg):
        return
    runtime = _resolve_runtime(hass, connection, msg)
    if runtime is None:
        return
    enabled = msg["enabled"]
    if enabled and not runtime.get("sim_fingerprints"):
        connection.send_error(
            msg["id"],
            "no_sim_fingerprints",
            "Generate dummy fingerprints first.",
        )
        return
    if enabled and runtime.get("survey", {}).get("recording"):
        connection.send_error(
            msg["id"], "recording", "Stop the survey before simulating."
        )
        return
    _apply_simulation(runtime, enabled)
    connection.send_result(msg["id"], {"simulation": runtime["simulation"]})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roomprint/sim_position",
        vol.Required("x"): vol.Coerce(float),
        vol.Required("y"): vol.Coerce(float),
        vol.Optional("entry_id"): str,
    }
)
@callback
def websocket_sim_position(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Feed one simulated phone position (metres) through the matcher."""
    if not _require_admin(connection, msg):
        return
    runtime = _resolve_runtime(hass, connection, msg)
    if runtime is None:
        return
    if not runtime.get("simulation"):
        connection.send_error(
            msg["id"], "simulation_off", "Simulation is not on."
        )
        return
    apply_live = runtime.get("apply_live")
    if apply_live is None:
        connection.send_error(
            msg["id"], "not_ready", "The room sensor is not ready yet."
        )
        return
    x = min(max(msg["x"], 0.0), 10.0)
    y = min(max(msg["y"], 0.0), 6.0)
    readings = readings_for_point(
        x, y, runtime.get("sim_geometries", {}), runtime["sim_rng"]
    )
    result = apply_live(readings, time.time())
    if result is None:
        connection.send_error(
            msg["id"], "not_ready", "The room sensor is not ready yet."
        )
        return
    connection.send_result(
        msg["id"],
        {
            "readings": {
                key: value for key, value in readings.items() if value is not None
            },
            "room": result["room"],
            "confidence": result["confidence"],
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roomprint/get_simulation",
        vol.Optional("entry_id"): str,
    }
)
@callback
def websocket_get_simulation(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Simulation state for the panel."""
    if not _require_admin(connection, msg):
        return
    runtime = _resolve_runtime(hass, connection, msg)
    if runtime is None:
        return
    connection.send_result(
        msg["id"],
        {
            "simulation": bool(runtime.get("simulation")),
            "has_sim_fingerprints": bool(runtime.get("sim_fingerprints")),
            "geometries": dict(runtime.get("sim_geometries", {})),
        },
    )
