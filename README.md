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
3. **Live matching.** Each device gets a room sensor (e.g.
   `sensor.roomprint_room`) reporting the current room, with confidence
   and margin attributes. It is event-driven: a new advertisement
   re-evaluates the room, a decisive win switches immediately (the
   under-2-seconds goal), an ambiguous win waits out a short dwell.
   When the phone is still, the matcher goes quiet.
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
   → Roomprint), name the device (e.g. "Alex's iPhone") and enter its
   Bluetooth address. Optionally link the device to a person (a
   dropdown of your Home Assistant people) — room events will report
   that person. If your phone's address rotates, add it to the Private
   BLE Device integration first so Home Assistant resolves it to one
   stable address — Roomprint tracks that address. To change the name,
   address, or linked person later, use Settings → Devices & services
   → Roomprint → Configure (the entry reloads and the sensor subscribes
   to the new address).
3. Open **Room Survey** in the sidebar and survey each room, doors open.

## Multiple people

One config entry tracks one phone, so a household adds one entry per
person: Settings → Devices & services → Add integration → Roomprint,
once per phone, each with its own name. The same Bluetooth address
cannot be added twice. Each entry gets its own room sensor (named
after the device, e.g. `sensor.alex_s_iphone_room`) and its own
`roomprint_room_changed` event carrying that device's address, label,
and linked person.

Two separate links connect a device to a person — don't mix them up:

- **Home / away: the device tracker.** Each entry also creates its own
  `device_tracker` (e.g. `device_tracker.alex_s_iphone_tracker`): its
  state is `home` while the phone is heard, `not_home` once it goes
  quiet for 3 minutes, and `unknown` before it is ever seen. The room
  is an `area` attribute on the tracker, not its state. Link each
  tracker to its person in Settings → People and that person's state
  follows it natively (home / not_home).
- **Who is in which room: the person picked in setup.** When you add
  (or Configure) an entry, pick the device's person from the dropdown
  of your Home Assistant people. That person entity — `person.alex`,
  say — is what the room sensor's `person` attribute and the
  `roomprint_room_changed` / `roomprint_live` events report. A device
  with no person picked reports `null`.

The typed name is only the device's display name (entry title, sensor
and tracker names). It never identifies the person — only the picked
`person.*` entity does.

The Room Survey panel has a device selector at the top: pick a device
and the Survey and Simulate tabs follow it — its room list, its live
signal bars, its room-change feed, and its simulation. Fingerprints
are shared: one survey per area covers every device, since the
fingerprint is a property of the room, not the phone.

## Simulator

No hardware yet? The **Simulate** tab in the panel (next to Survey)
validates the whole flow with dummy data. It maps your Home Assistant
areas onto a dummy floor plan by name (living, kitchen, master, guest,
bath, entrance), builds dummy fingerprints from a path-loss model, and lets you
drag a phone dot around an SVG map. The fake RSSI runs through the real
matcher and the real room sensor, so you can watch `sensor.roomprint_room`
change and room-change events fire as you drag across a boundary.

1. Open **Room Survey** → **Simulate**.
2. **Generate dummy fingerprints** (areas are matched by name; unmatched
   areas are skipped).
3. Turn simulation on and drag the phone dot around the map.

Simulation is in memory only: dummy fingerprints are never written to
storage, and real Bluetooth advertisements are ignored while it is on.
Turning it off restores the real fingerprints. A real survey cannot
start while simulation is on.

**What it validates:** the matcher, the room sensor, the room-change
events, the panel, and the survey/recording flow shape. **What it cannot
test:** the radio layer — the proxies, Home Assistant Bluetooth, the
advertisement callbacks, and your phone's actual advertisement rate.
Those need real hardware.

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
- `config_flow.py` — setup: one entry per device (name + Bluetooth
  address + optional native Person link, duplicate addresses refused).
  Options flow (Configure): change the name, address, or person after
  setup.
- `sensor.py` — the room sensor, fed by the Bluetooth advertisement stream.
- `device_tracker.py` — home/not_home tracker per device, for the native
  Person (room as its `area` attribute, never its state).
- `websocket.py` — the `roomprint/*` API used by the survey panel
  (`list_entries` + per-entry routing for survey and simulation).
- `matcher.py` — the pure matcher core (stdlib only, no Home Assistant).
- `simulate.py` — the dummy-map RSSI model (stdlib only). The panel sends
  positions; this module computes the readings.
- `frontend/roomprint-panel.js` — the survey panel.
