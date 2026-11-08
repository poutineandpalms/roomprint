"""Roomprint simulator model — pure functions, stdlib only.

Turns a phone position on a floor map into the RSSI values the room
sensors would hear, using a simple indoor path-loss model, and builds
dummy fingerprints from the map so the whole matcher -> sensor -> event
flow can be validated with no hardware at all.

The model lives here and nowhere else: the panel sends phone positions,
this module computes the readings. One model, tested once.
"""
from __future__ import annotations

import math
import random

try:
    from .matcher import compute_fingerprint
except ImportError:  # standalone use (tests): matcher/ on sys.path
    from matcher import compute_fingerprint

# Reference level: RSSI 1 m from the phone, and the indoor path-loss
# exponent. -59 dBm @ 1 m with n=2.2 gives roughly -70 dBm at 3 m and
# -77 dBm at 6 m — the same ballpark as real BLE room readings.
TX_POWER_DBM = -59.0
PATH_LOSS_EXPONENT = 2.2
JITTER_DB = 3.0        # per-reading noise, uniform +/- this
MIN_DISTANCE_M = 0.5   # closer than this counts as 0.5 m (no singularity)
HEARING_FLOOR_DBM = -92.0  # weaker than this: the sensor does not hear it
WALL_PENALTY_DB = 8.0  # a wall between phone and sensor costs ~8 dB; without
# it the map is one open hall and neighbouring rooms sit only ~0.3 apart
# in matcher score — below the decisive margin, so every room change
# would wait out the dwell instead of flipping instantly.

# Default map (metres, canvas 10 x 6). Keys are room slugs; the wiring
# maps real Home Assistant areas onto these by name. Sensors sit at the
# centre of their room's circle.
DEFAULT_MAP: dict[str, dict] = {
    "living":  {"x": 2.8, "y": 3.8, "radius": 2.3},
    "kitchen": {"x": 7.6, "y": 3.8, "radius": 1.9},
    "master":  {"x": 7.6, "y": 1.4, "radius": 1.7},
    "guest":   {"x": 2.6, "y": 1.3, "radius": 1.6},
    "bath":    {"x": 5.2, "y": 1.2, "radius": 1.0},
    # Small corridor at the bottom edge, off the living room.
    "entrance": {"x": 5.0, "y": 5.2, "radius": 0.9},
}


def rssi_for_distance(distance_m: float, rng: random.Random) -> float | None:
    """RSSI a sensor would report for a phone this far away, or None."""
    d = max(distance_m, MIN_DISTANCE_M)
    rssi = TX_POWER_DBM - 10 * PATH_LOSS_EXPONENT * math.log10(d)
    rssi += rng.uniform(-JITTER_DB, JITTER_DB)
    if rssi < HEARING_FLOOR_DBM:
        return None
    return round(rssi)


def _room_containing(
    x: float, y: float, geometries: dict[str, dict]
) -> str | None:
    """Room whose circle contains (x, y); closest centre wins on overlap."""
    containing = [
        (math.hypot(x - geo["x"], y - geo["y"]), area_id)
        for area_id, geo in geometries.items()
        if math.hypot(x - geo["x"], y - geo["y"]) <= geo["radius"]
    ]
    if not containing:
        return None
    return min(containing)[1]


def readings_for_point(
    x: float, y: float, geometries: dict[str, dict], rng: random.Random
) -> dict[str, float | None]:
    """Live vector for a phone at (x, y): {area_id: rssi_or_None}.

    A sensor in another room than the phone reads WALL_PENALTY_DB weaker
    — the wall between them. The same rule applies when the dummy
    fingerprints are generated, so live vectors and fingerprints agree.
    """
    phone_room = _room_containing(x, y, geometries)
    readings: dict[str, float | None] = {}
    for area_id, geo in geometries.items():
        distance = math.hypot(x - geo["x"], y - geo["y"])
        rssi = rssi_for_distance(distance, rng)
        if (
            rssi is not None
            and phone_room is not None
            and area_id != phone_room
        ):
            rssi -= WALL_PENALTY_DB
            if rssi < HEARING_FLOOR_DBM:
                rssi = None
        readings[area_id] = rssi
    return readings


def _random_point_in_circle(
    cx: float, cy: float, radius: float, rng: random.Random
) -> tuple[float, float]:
    """Uniform random point inside a circle (area-weighted)."""
    angle = rng.uniform(0, 2 * math.pi)
    dist = radius * math.sqrt(rng.random())
    return cx + dist * math.cos(angle), cy + dist * math.sin(angle)


def generate_fingerprints(
    geometries: dict[str, dict],
    samples_per_room: int = 120,
    seed: int = 0,
) -> dict[str, dict]:
    """Dummy fingerprints from the map: for each room, sample random
    points inside its circle, compute what every sensor would hear,
    and aggregate with the real compute_fingerprint."""
    rng = random.Random(seed)
    fingerprints: dict[str, dict] = {}
    for area_id, geo in geometries.items():
        samples = []
        for _ in range(samples_per_room):
            px, py = _random_point_in_circle(
                geo["x"], geo["y"], geo["radius"], rng
            )
            samples.append(
                {"readings": readings_for_point(px, py, geometries, rng)}
            )
        fingerprints[area_id] = compute_fingerprint(samples)
    return fingerprints
