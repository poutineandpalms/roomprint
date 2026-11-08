# Roomprint

Bluetooth room fingerprints for Home Assistant. Walk each room once with
your phone; Roomprint learns what the room *sounds* like in Bluetooth
signal, then tells Home Assistant which room your phone is in — fast
enough to switch the moment you walk through a doorway.

This is the successor to the discontinued Groupon smart-plug system:
fixed Bluetooth scanners (one per room) listen for your phone, and a
per-room signal fingerprint decides the room. No floor plan, no map, no
triangulation — just fingerprints.

## How it works

1. **Scanners.** One ESP32 running the ESPHome Bluetooth Proxy in each
   room, assigned to that room's Home Assistant area. (Shelly Plus
   devices with Bluetooth proxying also work.) Home Assistant collects
   every advertisement they hear; Roomprint subscribes to that stream.
2. **Survey.** Open **Room Survey** in the sidebar, pick a room, walk it
   slowly — perimeter, middle, linger at the doorway — and stop. The
   walk becomes one fixed-size fingerprint per room: per scanner, the
   median, 10th and 90th percentile, and detection rate.
3. **Live matching.** A `sensor.roomprint_room` entity reports the current
   room, with confidence and margin attributes. It is event-driven: a new
   advertisement re-evaluates the room, a decisive win switches
   immediately (the under-2-seconds goal), an ambiguous win waits out a
   short dwell. When the phone is still, the matcher goes quiet.
4. **Automations.** Use the sensor like any other: lights, speakers,
   whatever you want, per room.

## Install

**Manual (v0.1):** copy `custom_components/roomprint/` into your Home
Assistant configuration folder's `custom_components/`, then restart Home
Assistant (first install with a config flow loads without a restart;
updates need one).

**HACS (later):** add this repository as a custom repository in HACS.
The repository does not exist yet — manual install is the only path
for now.

## Setup

1. Create one Home Assistant area per room (Settings → Areas & zones)
   and assign one Bluetooth proxy to each.
2. Add the integration (Settings → Devices & services → Add integration
   → Roomprint) and enter the Bluetooth address of the device to track
   (your phone). If your phone's address rotates, add it to the Private
   BLE Device integration first so Home Assistant resolves it to one
   stable address — Roomprint tracks that address.
3. Open **Room Survey** in the sidebar and survey each room, doors open.

## Local only

Roomprint makes no network calls outside Home Assistant. No cloud
service, no public URL, no tunnel, no account. The panel is served from
inside Home Assistant, fingerprints live in Home Assistant storage, and
the matcher runs in Home Assistant's own process. Remote access is
whatever you already use for Home Assistant (local IP at home, your own
tunnel / Nabu Casa / VPN away) — Roomprint never introduces one.

**Acceptance test:** unplug the WAN cable (or block the box's internet)
and run a full survey plus live room detection. Everything is phone →
Home Assistant over your LAN. If any step needs the internet, that is a
bug in Roomprint, not a requirement.

## Files

- `manifest.json` — integration metadata (`requirements: []`,
  `iot_class: calculated`, config flow).
- `__init__.py` — setup, storage, sidebar panel registration.
- `config_flow.py` — one step: the tracked device's Bluetooth address.
- `sensor.py` — the room sensor, fed by the Bluetooth advertisement stream.
- `websocket.py` — the `roomprint/*` API used by the survey panel.
- `matcher.py` — the pure matcher core (stdlib only, no Home Assistant).
- `frontend/roomprint-panel.js` — the survey panel.
