"""Look up port names for GPS positions via OpenStreetMap/Nominatim reverse geocoding.

Uses only the Python standard library (urllib), no extra dependency. Results are cached
locally (keyed on rounded coordinates) so repeated runs don't send new requests, respecting
Nominatim's usage policy (max. 1 request/second, identifiable User-Agent). For heavy/commercial
use, consider running your own Nominatim instance or a paid geocoding service.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_MIN_INTERVAL_S = 1.0
_EARTH_RADIUS_M = 6_371_000.0

# How far to look for a named marina before falling back to Nominatim's own plain address match
# (village/town/...) -- same radius as _NEARBY_THRESHOLD_M below, so a marina that's this close
# is shown outright (no "aan de kant, bij" prefix) the same way a directly-matched feature would
# be within that threshold.
_MARINA_SEARCH_RADIUS_M = 250.0

# Beyond this distance from the matched feature, the name is no longer really "the" place we're
# at -- it's just the closest one Nominatim could find (common at anchor, away from any mapped
# harbour) -- so it gets prefixed to say so instead of silently implying we're right there.
_NEARBY_THRESHOLD_M = 250.0

# OSM feature types that mean "somewhere boats actually tie up", used to distinguish the two
# prefixes for a far-away match: "aan de kant, bij" (alongside, near) vs "op het water, bij" (on
# the water, near). This is a coarse heuristic, not a real "am I touching the shore" measurement
# (no coastline data available) -- e.g. a nearby coastal path or generic pier doesn't necessarily
# mean the boat is alongside it, so those deliberately stay in the "on the water" bucket.
_MOORING_TYPES = {"marina", "harbour", "quay", "mooring", "yacht_club", "boatyard"}

_PREFERRED_ADDRESS_KEYS = (
    "leisure",
    "marina",
    "harbour",
    "town",
    "village",
    "city",
    "municipality",
    "suburb",
    "quarter",
)


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _nearby_marina_name(lat: float, lon: float, user_agent: str) -> Optional[str]:
    """Looks for the nearest named marina (OSM leisure=marina) within _MARINA_SEARCH_RADIUS_M via
    Overpass, since Nominatim's own reverse geocode only matches a feature the query point falls
    *inside* -- a boat moored just outside a marina's own mapped basin (found in practice: at the
    harbour mouth, not mid-basin) never gets the marina's name from a plain reverse lookup, only
    whatever unrelated point feature happens to be nearest instead. A separate service (not
    Nominatim) since Nominatim's own search endpoint can't filter by OSM tag, only match free-text
    against a name we'd have to already know."""
    query = (
        "[out:json][timeout:10];"
        f'way(around:{_MARINA_SEARCH_RADIUS_M:.0f},{lat:.6f},{lon:.6f})["leisure"="marina"]["name"];'
        "out center tags;"
    )
    data = urllib.parse.urlencode({"data": query}).encode()
    request = urllib.request.Request(_OVERPASS_URL, data=data, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None  # best-effort: falls back to the plain Nominatim result, not cached as a name

    best_name: Optional[str] = None
    best_distance: Optional[float] = None
    for element in payload.get("elements", []):
        name = element.get("tags", {}).get("name")
        center = element.get("center")
        if not name or not center:
            continue
        distance = _distance_m(lat, lon, center["lat"], center["lon"])
        if best_distance is None or distance < best_distance:
            best_name, best_distance = name, distance
    return best_name


class Geocoder:
    def __init__(
        self,
        *,
        cache_file: Optional[Path] = None,
        user_agent: str = "nmea2000processor/0.1 (personal sailing logbook)",
        language: str = "nl",
        precision: int = 4,
    ) -> None:
        self.cache_file = cache_file
        self.user_agent = user_agent
        self.language = language
        self.precision = precision
        self._cache: dict[str, str] = {}
        self._last_request = 0.0
        if cache_file is not None and cache_file.exists():
            self._cache = json.loads(cache_file.read_text(encoding="utf-8"))

    def _key(self, lat: float, lon: float) -> str:
        return f"{round(lat, self.precision)},{round(lon, self.precision)}"

    def place_name(self, lat: float, lon: float) -> str:
        key = self._key(lat, lon)
        if key in self._cache:
            return self._cache[key]
        name, cacheable = self._lookup(lat, lon)
        if cacheable:
            self._cache[key] = name
            self._save_cache()
        return name

    def _lookup(self, lat: float, lon: float) -> Tuple[str, bool]:
        """Returns (name, cacheable). A failed request (no internet, DNS down, Nominatim
        unreachable, ...) is not cacheable -- it's a transient environmental problem, not a fact
        about that position, so it must not be written to the cache file: otherwise a single
        offline run permanently poisons that position with "geocoding failed", and even a later
        run with a working connection would just keep returning the same stale failure forever
        instead of retrying (found in practice: ran once without internet on the boat)."""
        wait = _MIN_INTERVAL_S - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

        params = urllib.parse.urlencode(
            {
                "format": "jsonv2",
                "lat": f"{lat:.6f}",
                "lon": f"{lon:.6f}",
                # Nominatim's "zoom" also limits which feature types are even considered, not
                # just the search radius -- 16 ("major streets") excludes small islets/hamlets
                # entirely, so in sparsely-mapped water (e.g. Golfe du Morbihan) it can return a
                # named feature over a kilometer away in favor of nothing closer being eligible.
                # 18 ("building" level) considers much smaller/closer features (found in
                # practice: 1.1 km away -> ~250 m away for the same anchor position), without
                # regressing the marina lookups this app relies on most (leisure=marina areas are
                # still the nearest eligible feature at a real harbor either way).
                "zoom": 18,
                "addressdetails": 1,
                "accept-language": self.language,
            }
        )
        request = urllib.request.Request(
            f"{_NOMINATIM_URL}?{params}", headers={"User-Agent": self.user_agent}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            self._last_request = time.monotonic()
            return f"Unknown ({lat:.4f}, {lon:.4f}) [geocoding failed: {exc}]", False

        self._last_request = time.monotonic()
        # A nearby marina's own name beats whatever village/town Nominatim's plain reverse lookup
        # happened to match (found in practice: moored just outside the marina's own mapped basin,
        # so Nominatim matched an unrelated nearby feature and used *its* address hierarchy
        # instead -- see _nearby_marina_name).
        marina_name = _nearby_marina_name(lat, lon, self.user_agent)
        self._last_request = time.monotonic()
        if marina_name:
            return marina_name, True
        return _describe_place(payload, lat, lon), True

    def _save_cache(self) -> None:
        if self.cache_file is None:
            return
        self.cache_file.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class NoGeocoder:
    """Skips the online lookup and shows the coordinates instead."""

    def place_name(self, lat: float, lon: float) -> str:
        return f"{lat:.4f}, {lon:.4f}"


def _pick_place_name(payload: dict, lat: float, lon: float) -> str:
    if payload.get("category") == "leisure" and payload.get("name"):
        return payload["name"]

    address = payload.get("address", {})
    for key in _PREFERRED_ADDRESS_KEYS:
        if key in address:
            return address[key]

    name = payload.get("name")
    if name:
        return name

    display_name = payload.get("display_name")
    if display_name:
        return display_name.split(",")[0]

    return f"Unknown ({lat:.4f}, {lon:.4f})"


def _describe_place(payload: dict, lat: float, lon: float) -> str:
    """Prefixes the picked name with "aan de kant, bij" (alongside, near) or "op het water, bij"
    (on the water, near) when the matched feature is more than ``_NEARBY_THRESHOLD_M`` away from
    the actual position -- otherwise the name reads as if we were right there, e.g. showing a
    village name for a position that was really anchored ~250 m offshore of it."""
    name = _pick_place_name(payload, lat, lon)
    try:
        feature_lat = float(payload["lat"])
        feature_lon = float(payload["lon"])
    except (KeyError, TypeError, ValueError):
        return name

    if _distance_m(lat, lon, feature_lat, feature_lon) <= _NEARBY_THRESHOLD_M:
        return name

    prefix = "aan de kant, bij" if payload.get("type") in _MOORING_TYPES else "op het water, bij"
    return f"{prefix} {name}"
