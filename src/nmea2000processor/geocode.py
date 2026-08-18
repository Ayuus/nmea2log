"""Look up port names for GPS positions via OpenStreetMap/Nominatim reverse geocoding.

Uses only the Python standard library (urllib), no extra dependency. Results are cached
locally (keyed on rounded coordinates) so repeated runs don't send new requests, respecting
Nominatim's usage policy (max. 1 request/second, identifiable User-Agent). For heavy/commercial
use, consider running your own Nominatim instance or a paid geocoding service.
"""

from __future__ import annotations

import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from .log import log

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_MIN_INTERVAL_S = 1.0
_EARTH_RADIUS_M = 6_371_000.0

# How far to look for a named landmark (marina, islet, ...) before falling back to Nominatim's
# own plain address match (village/town/...) -- shown outright within this radius (no "aan de
# kant, bij"/"op het water, bij" prefix), the same as a directly-matched feature would be within
# _NEARBY_THRESHOLD_M. Deliberately a bit wider than that: an anchorage is more often a bit off
# from the landmark it's named after (e.g. a boat anchored off an islet, not on it) than a marina
# berth is from the marina itself (found in practice: anchored 276 m off Île de la Jument).
_LANDMARK_SEARCH_RADIUS_M = 300.0

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


_LANDMARK_MAX_RETRIES = 10


def _nearby_landmark_name(lat: float, lon: float, user_agent: str) -> Tuple[Optional[str], bool]:
    """Looks for the nearest named marina or islet within _LANDMARK_SEARCH_RADIUS_M via Overpass,
    since Nominatim's own reverse geocode only matches a feature the query point falls *inside*
    (an area, e.g. a marina) or literally nearest to (a point, e.g. an islet's own marker) --
    neither necessarily wins over an unrelated, closer-by-Nominatim's-own-metric feature. Found
    in practice twice: moored just outside a marina's own mapped basin (Nominatim matched an
    unrelated nearby slipway instead, using *its* address hierarchy, a village over a km away);
    anchored a couple hundred meters off a named islet (Nominatim matched an unrelated pier
    instead, using a real but different nearby hamlet). A separate service (not Nominatim) since
    Nominatim's own search endpoint can't filter by OSM tag, only match free-text against a name
    we'd have to already know.

    Returns ``(name, ok)`` -- ``ok`` is False only if the check itself never completed (every
    retry failed), as opposed to completing and simply finding nothing nearby, so the caller can
    tell the two apart: a confirmed "nothing here" is safe to cache, a failed check is not (the
    caller should retry it on a later run instead of being stuck with today's plain Nominatim
    fallback forever -- see Geocoder._lookup)."""
    query = (
        "[out:json][timeout:10];"
        "("
        f'way(around:{_LANDMARK_SEARCH_RADIUS_M:.0f},{lat:.6f},{lon:.6f})["leisure"="marina"]["name"];'
        f'node(around:{_LANDMARK_SEARCH_RADIUS_M:.0f},{lat:.6f},{lon:.6f})["place"="islet"]["name"];'
        f'way(around:{_LANDMARK_SEARCH_RADIUS_M:.0f},{lat:.6f},{lon:.6f})["place"="islet"]["name"];'
        ");"
        "out center tags;"
    )
    data = urllib.parse.urlencode({"data": query}).encode()
    payload = None
    # The free public Overpass instance occasionally answers a plain around-query with a 504
    # under load and nothing else wrong (found in practice: failed once, succeeded < 1 s later)
    # -- worth retrying a good few times before giving up and falling back to Nominatim's own,
    # less specific match.
    for attempt in range(_LANDMARK_MAX_RETRIES + 1):
        if attempt:
            time.sleep(2.0)
        request = urllib.request.Request(_OVERPASS_URL, data=data, headers={"User-Agent": user_agent})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        # OSError alongside URLError: some connection failures (e.g. the server dropping the
        # connection mid-response) surface as a raw ConnectionResetError/http.client exception,
        # not wrapped in URLError (found in practice: http.client.RemoteDisconnected crashed the
        # whole run instead of triggering the fallback below).
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(
                f"[geocode] Overpass landmark check failed ({exc}) "
                f"-- attempt {attempt + 1}/{_LANDMARK_MAX_RETRIES + 1}",
                file=sys.stderr,
            )
    if payload is None:
        return None, False  # best-effort: caller falls back to the plain Nominatim result

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
    return best_name, True


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
        instead of retrying (found in practice: ran once without internet on the boat). Same
        reasoning applies if only the separate landmark check (_nearby_landmark_name) fails after
        exhausting its own retries: this run's name is still usable (Nominatim's own plain match),
        just not cacheable, so a later run retries the landmark check instead of being stuck with
        today's possibly-wrong fallback forever."""
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
        # OSError alongside URLError: some connection failures (e.g. the server dropping the
        # connection mid-response) surface as a raw ConnectionResetError/http.client exception,
        # not wrapped in URLError (found in practice: http.client.RemoteDisconnected crashed the
        # whole run instead of being treated as a normal, non-cacheable lookup failure).
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._last_request = time.monotonic()
            return f"Unknown ({lat:.4f}, {lon:.4f}) [geocoding failed: {exc}]", False

        self._last_request = time.monotonic()
        # A nearby marina or islet's own name beats whatever village/town Nominatim's plain
        # reverse lookup happened to match instead -- see _nearby_landmark_name.
        landmark_name, landmark_check_ok = _nearby_landmark_name(lat, lon, self.user_agent)
        self._last_request = time.monotonic()
        if landmark_name:
            return landmark_name, True
        place = _describe_place(payload, lat, lon)
        # Not cacheable if the landmark check itself failed (as opposed to completing and simply
        # finding nothing nearby): otherwise this run's plain Nominatim fallback -- possibly the
        # wrong name, that's the whole reason the check exists -- would get permanently stuck in
        # the cache even once Overpass is reachable again on a later run.
        return place, landmark_check_ok

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
