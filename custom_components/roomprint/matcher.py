"""Roomprint matcher core — pure functions, stdlib only, no Home Assistant.

Design (agreed with Alex, Nov 8 2026):
- One fingerprint per room. Record ALL sensors at survey time; top-N is a
  tunable in the matching logic, never baked into the fingerprint.
- Per sensor, a fingerprint stores 4 numbers: median, 10th percentile,
  90th percentile, detection rate. Fixed size whatever the walk duration.
- Matching: interval penalty per sensor (0 inside [p10, p90], growing
  outside it), a missing-reading penalty scaled by detection rate, and
  drop-the-worst-sensor forgiveness — but only for a sensor reading much
  WEAKER than expected. A door can only attenuate a signal, never
  strengthen it, so a strong reading is evidence and is never forgiven.
- Switching is two-tier: a decisive win switches immediately (the <2s
  goal); an ambiguous win waits out a dwell timer.
- Quiet mode: an identical live vector short-circuits — nothing to decide.
"""

from __future__ import annotations

import math
import statistics

# Tunables (to be tuned empirically once real survey data exists).
TOP_N = 5               # live sensors evaluated at match time (strongest first)
DECISIVE_MARGIN = 0.5   # score gap that switches rooms immediately
DWELL_SECONDS = 3.0     # wait this long before an ambiguous switch
HIGH_DETECTION = 0.8    # a sensor heard this often counts as "usually here"


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile of an already-sorted list."""
    if not sorted_values:
        raise ValueError("no values")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct / 100.0 * (len(sorted_values) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return sorted_values[int(rank)]
    frac = rank - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def compute_fingerprint(samples: list[dict]) -> dict[str, dict]:
    """Aggregate raw survey samples into one fixed-size fingerprint.

    samples: [{readings: {scanner_id: rssi_or_None}}, ...] — one entry per
    recorded moment of the walk. Returns {scanner_id: {median, p10, p90,
    detection_rate}}. Size depends only on the number of scanners, never
    on how many samples the walk produced.
    """
    if not samples:
        raise ValueError("no samples")
    scanners: set[str] = set()
    for sample in samples:
        scanners.update(sample.get("readings", {}).keys())
    fingerprint: dict[str, dict] = {}
    for scanner in sorted(scanners):
        values = [
            s["readings"][scanner]
            for s in samples
            if s.get("readings", {}).get(scanner) is not None
        ]
        if not values:
            # Never heard at all during this survey: keep it, detection 0.
            fingerprint[scanner] = {
                "median": None,
                "p10": None,
                "p90": None,
                "detection_rate": 0.0,
            }
            continue
        ordered = sorted(values)
        fingerprint[scanner] = {
            "median": statistics.median(values),
            "p10": _percentile(ordered, 10),
            "p90": _percentile(ordered, 90),
            "detection_rate": len(values) / len(samples),
        }
    return fingerprint


def _sensor_penalty(reading: float | None, stats: dict) -> tuple[float, bool]:
    """(penalty, is_weak) for one sensor: 0 = fits the fingerprint.

    is_weak means the sensor hears the phone much more weakly than this
    room's fingerprint expects — the closed-door signature. A door can
    only attenuate a signal, never strengthen it, so only a weak reading
    is ever forgiven (see score_room); a strong reading is evidence.
    """
    if stats["median"] is None:
        return 0.0, False  # sensor never heard here; it says nothing
    if reading is None:
        # Missing is censored data, not a zero: it matters exactly as much
        # as this sensor usually hears the phone in this room. Missing is
        # also a weak reading — a closed door can silence a sensor.
        return 2.0 * stats["detection_rate"], True
    p10, p90, median = stats["p10"], stats["p90"], stats["median"]
    interval = max(p90 - p10, 1.0)
    if p10 <= reading <= p90:
        return 0.5 * abs(reading - median) / interval, False
    outside = min(abs(reading - p10), abs(reading - p90))
    return 1.0 + outside / interval, reading < p10


def score_room(live: dict[str, float | None], fingerprint: dict, top_n: int = TOP_N) -> float:
    """Score one room against a live vector. Lower = better fit.

    Only the top-N strongest live readings are evaluated; a fingerprint
    sensor with a high detection rate that is silent right now still
    counts against the room. The single worst sensor penalty is dropped
    (closed-door forgiveness).
    """
    present = sorted(
        ((scanner, rssi) for scanner, rssi in live.items() if rssi is not None),
        key=lambda item: item[1],
        reverse=True,
    )[:top_n]
    penalties: list[tuple[float, bool]] = []
    for scanner, rssi in present:
        stats = fingerprint.get(scanner)
        if stats is None:
            continue
        penalties.append(_sensor_penalty(rssi, stats))
    live_scanners = {scanner for scanner, _ in present}
    for scanner, stats in fingerprint.items():
        if scanner in live_scanners:
            continue
        if stats["detection_rate"] >= HIGH_DETECTION:
            penalties.append(_sensor_penalty(None, stats))
    if not penalties:
        return float("inf")
    if len(penalties) >= 2:
        # Forgive the single worst WEAK sensor — a door can only
        # attenuate, so a weak reading is the closed-door signature.
        # A sensor reading much STRONGER than this room expects is
        # positive evidence against the room and is never forgiven:
        # dropping it would blur neighbouring rooms together. Note the
        # drop picks the worst weak penalty, not the worst overall, so
        # one strong outlier cannot block forgiveness of the weak one.
        weak = [item for item in penalties if item[1]]
        if weak:
            penalties.remove(max(weak, key=lambda item: item[0]))
    return sum(penalty for penalty, _ in penalties) / len(penalties)


class RoomMatcher:
    """Holds fingerprints, turns live vectors into a room + confidence.

    evaluate() is meant to be called on every new advertisement. It is
    cheap (a few dozen arithmetic ops) and short-circuits when the live
    vector is identical to the previous one — the quiet mode.
    """

    def __init__(
        self,
        fingerprints: dict[str, dict],
        top_n: int = TOP_N,
        decisive_margin: float = DECISIVE_MARGIN,
        dwell_seconds: float = DWELL_SECONDS,
    ) -> None:
        self.fingerprints = fingerprints
        self.top_n = top_n
        self.decisive_margin = decisive_margin
        self.dwell_seconds = dwell_seconds
        self.current_room: str | None = None
        self._pending_room: str | None = None
        self._pending_since: float | None = None
        self._last_live: dict | None = None
        self.evaluations = 0  # counts real (non-short-circuited) evaluations

    def evaluate(self, live: dict[str, float | None], now: float) -> dict:
        """Return {room, confidence, margin, scores}. `now` is seconds."""
        if live == self._last_live:
            return self._result()
        self._last_live = dict(live)
        self.evaluations += 1
        scores = {
            room: score_room(live, fp, self.top_n)
            for room, fp in self.fingerprints.items()
        }
        ranked = sorted(scores.items(), key=lambda item: item[1])
        best_room, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else float("inf")
        margin = second_score - best_score
        if self.current_room is None:
            self.current_room = best_room
            self._pending_room = None
        elif best_room != self.current_room:
            if margin >= self.decisive_margin:
                # Decisive win: switch now, no waiting for a timer.
                self.current_room = best_room
                self._pending_room = None
            else:
                # Ambiguous: only switch if it stays the best for the dwell.
                if self._pending_room != best_room:
                    self._pending_room = best_room
                    self._pending_since = now
                elif now - (self._pending_since or now) >= self.dwell_seconds:
                    self.current_room = best_room
                    self._pending_room = None
        else:
            self._pending_room = None
        return self._result(scores, margin)

    def _result(self, scores: dict | None = None, margin: float | None = None) -> dict:
        return {
            "room": self.current_room,
            "confidence": self._confidence(margin),
            "margin": margin,
            "scores": scores,
        }

    @staticmethod
    def _confidence(margin: float | None) -> float | None:
        if margin is None or margin == float("inf"):
            return None
        return margin / (margin + 1.0)
