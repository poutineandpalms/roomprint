"""WebSocket API for the Room Survey panel — admin only, all local.

Commands (all under the ``roomprint/`` namespace):
- roomprint/list_areas        -> [{area_id, name, surveyed}]
- roomprint/start_survey      {area_id} -> {area_id, started_at}
- roomprint/stop_survey       -> {area_id, sample_count, fingerprint}
- roomprint/get_survey_status -> {recording, area_id, started_at, sample_count}
- roomprint/get_fingerprints  -> {area_id: fingerprint}

The survey records the tracked device's advertisements with
``async_register_advertisement_callback`` (every packet, per scanner),
snapshotting the latest per-scanner RSSI at most twice a second so a
walk stays a few hundred samples, not tens of thousands.
"""

from __future__ import annotations

import time
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth, websocket_api
from homeassistant.components.websocket_api import ActiveConnection
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar

from .const import DOMAIN
from .matcher import compute_fingerprint
from .util import RSSI_UNKNOWN, scanner_key

# Snapshot the live readings into a survey sample at most this often.
SAMPLE_MIN_INTERVAL = 0.5  # seconds


def _get_runtime(hass: HomeAssistant) -> dict[str, Any] | None:
    """Runtime of the single Roomprint config entry, or None if unset."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        return None
    return hass.data.get(DOMAIN, {}).get(entries[0].entry_id)


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
    websocket_api.async_register_command(hass, websocket_list_areas)
    websocket_api.async_register_command(hass, websocket_start_survey)
    websocket_api.async_register_command(hass, websocket_stop_survey)
    websocket_api.async_register_command(hass, websocket_get_survey_status)
    websocket_api.async_register_command(hass, websocket_get_fingerprints)


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
    runtime = _get_runtime(hass)
    if runtime is None:
        connection.send_error(msg["id"], "not_configured", "Roomprint is not set up.")
        return
    survey = runtime["survey"]
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


@websocket_api.websocket_command({vol.Required("type"): "roomprint/stop_survey"})
@websocket_api.async_response
async def websocket_stop_survey(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Stop the survey, compute and save the fingerprint."""
    if not _require_admin(connection, msg):
        return
    runtime = _get_runtime(hass)
    if runtime is None:
        connection.send_error(msg["id"], "not_configured", "Roomprint is not set up.")
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
    {vol.Required("type"): "roomprint/get_survey_status"}
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
    runtime = _get_runtime(hass)
    if runtime is None:
        connection.send_error(msg["id"], "not_configured", "Roomprint is not set up.")
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
