"""The fictional cruise of the demo logbook and of the demo .ebl files: one definition, used by generate_demo_logbook.py (the
page examples/demo-logbook.html) and generate_demo_ebl.py (the files in examples/demo-data/), so importing the files builds the
same five trips the demo page shows.

A made-up Wadden Sea cruise of a made-up boat: real, well-known public harbours (not identifying of any boat or owner), made-up
dates and times. Distances come from the route itself, so the numbers on the demo page, in the files and in what the app builds
from them agree."""

import math
from datetime import datetime, timedelta
from typing import List, Tuple

STOPS = [
    ("Enkhuizen", 52.7040, 5.2913),
    ("Medemblik", 52.7690, 5.1050),
    ("Den Oever", 52.9330, 5.0300),
    ("Oudeschild (Texel)", 53.0400, 4.8460),
    ("West-Terschelling", 53.3610, 5.2230),
    ("Enkhuizen", 52.7040, 5.2913),
]

# Extra via-points per leg so the route follows open water instead of a straight point-to-point line -- a pure straight line
# cuts across land in several places here (e.g. Enkhuizen-Medemblik crosses the Andijk peninsula, Medemblik-Den Oever crosses the
# Wieringermeer polder) -- found by checking each leg's rendered map against OpenStreetMap.
VIA_POINTS: List[List[Tuple[float, float]]] = [
    [(52.76, 5.33)],                   # Enkhuizen -> Medemblik: around the Andijk peninsula
    [(52.85, 5.25)],                   # Medemblik -> Den Oever: around the Wieringermeer coast
    [(53.02, 4.92)],                   # Den Oever -> Oudeschild: through the open Waddenzee
    [(53.305, 5.205)],                 # Oudeschild -> West-Terschelling: through the Vliestroom gap
    [(53.075, 5.341), (52.85, 5.30)],  # West-Terschelling -> Enkhuizen: through the Kornwerderzand lock (the only gap in the
]                                      # Afsluitdijk near here; a waypoint that is not at the lock leaves a segment across the dijk)

# How long each leg takes (hours, under way), chosen so the average speed is 5-6 knots.
DURATIONS_H = (2.3, 3.0, 1.8, 4.3, 6.8)
# The shallowest water on each leg (metres).
MIN_DEPTH_M = (2.8, 3.5, 4.1, 6.2, 5.4)
# Fuel burnt per nautical mile (litres): about 1.9 litres an hour at 5.3 knots.
FUEL_PER_NM = 0.36
# The meter of the (only) engine at the start of the cruise (hours).
ENGINE_HOURS_AT_START = 1200.0

START = datetime(2025, 6, 14, 8, 0, 0)  # departure of the first leg (UTC)
OVERNIGHT_H = 20  # between the arrival of a leg and the departure of the next


def distance_nm(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    half = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * math.asin(math.sqrt(half)) * 3440.065


def waypoints(leg: int) -> List[Tuple[float, float]]:
    """The route of a leg: departure, via-points, arrival."""
    return [STOPS[leg][1:], *VIA_POINTS[leg], STOPS[leg + 1][1:]]


def leg_distance_nm(leg: int) -> float:
    points = waypoints(leg)
    return sum(distance_nm(points[k], points[k + 1]) for k in range(len(points) - 1))


def leg_times(leg: int) -> Tuple[datetime, datetime]:
    """(departure, arrival) of a leg."""
    depart = START
    for earlier in range(leg):
        depart += timedelta(hours=DURATIONS_H[earlier] + OVERNIGHT_H)
    return depart, depart + timedelta(hours=DURATIONS_H[leg])
