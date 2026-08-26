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


_LANDMARK_MAX_RETRIES = 9

# Address levels that count as "a village name" -- used by _pick_place_name (a real village/
# town/city beats a leisure match that's just a bare point node, see there) and shared here so
# anything else that needs the same definition of "a real village/town/city" can reuse it.
_VILLAGE_LEVEL_ADDRESS_KEYS = ("town", "village", "city", "municipality", "suburb", "quarter")


def _nearby_islet_name(lat: float, lon: float, user_agent: str) -> Tuple[Optional[str], bool]:
    """Looks for the nearest named islet within _LANDMARK_SEARCH_RADIUS_M via Overpass, since
    Nominatim's own reverse geocode doesn't recognize islets at all -- it matches whatever
    unrelated feature happens to be nearest by its own metric instead (found in practice:
    anchored a couple hundred meters off a named islet, Nominatim matched an unrelated pier
    instead, using a real but different nearby hamlet). A separate service (not Nominatim) since
    Nominatim's own search endpoint can't filter by OSM tag, only match free-text against a name
    we'd have to already know.

    This used to also look for nearby marinas (to override a wrong or missing plain Nominatim
    match), but that's been dropped: it duplicated work Nominatim's own address usually already
    does today, and the remaining gap -- a marina genuinely missing from Nominatim's own address
    entirely -- is meant to be fixed by improving OpenStreetMap's own data for that spot, not
    worked around here.

    Only a plain name *node* is used, never the coastline *way* that usually also exists for the
    same islet -- found in practice: anchored well within a pier's own address match (38 m away,
    genuinely at that harbour), yet a large islet's coastline way still had *some* point of its
    shape within the search radius, so its own "center" (the way's geometric centroid, not the
    nearest point of its actual coastline) ended up nearly 680 m away and wrongly won outright
    over the harbour the boat was actually moored at. A node's coordinates are exact and single,
    so it doesn't have this problem -- a way's reported distance can be arbitrarily misleading
    for a large/elongated shape, so it's not trusted for this at all, even as a fallback.

    Returns ``(name, ok)`` -- ``ok`` is False only if the check itself never completed (every
    retry failed), as opposed to completing and simply finding nothing nearby, so the caller can
    tell the two apart: a confirmed "nothing here" is safe to cache, a failed check is not (the
    caller should retry it on a later run instead of being stuck with today's plain Nominatim
    fallback forever -- see Geocoder._lookup)."""
    # [out:json] is required -- without it Overpass answers with its own default format (XML,
    # HTTP 200) instead of an error, so a missing/dropped [out:json] fails json.loads on every
    # single request instead of just occasionally under load (found in practice: silently
    # reintroduced while simplifying this query, turned every real run's islet check into a
    # guaranteed failure until the retries gave up).
    query = (
        f'[out:json][timeout:10];'
        f'node(around:{_LANDMARK_SEARCH_RADIUS_M:.0f},{lat:.6f},{lon:.6f})'
        '["place"="islet"]["name"];'
        "out tags;"
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
        status = None
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status = response.status
                payload = json.loads(response.read().decode("utf-8"))
            break
        # OSError alongside URLError: some connection failures (e.g. the server dropping the
        # connection mid-response) surface as a raw ConnectionResetError/http.client exception,
        # not wrapped in URLError (found in practice: http.client.RemoteDisconnected crashed the
        # whole run instead of triggering the fallback below). The HTTP status is included
        # whenever one is known (an HTTPError's own code, or the status of a response that came
        # back but failed to parse as JSON) -- found in practice: a 200-with-XML failure and a
        # real server error both raised a bare-looking exception, indistinguishable in the log
        # without the status alongside it.
        except (urllib.error.URLError, OSError, ValueError) as exc:
            status = getattr(exc, "code", status)
            log(
                f"[geocode] Overpass islet check failed ({exc}) [http {status}] "
                f"-- attempt {attempt + 1}/{_LANDMARK_MAX_RETRIES + 1}",
                file=sys.stderr,
            )
    if payload is None:
        return None, False  # best-effort: caller falls back to the plain Nominatim result

    best_node: Optional[Tuple[str, float]] = None
    for element in payload.get("elements", []):
        tags = element.get("tags", {})
        if tags.get("place") != "islet":
            continue
        name = tags.get("name")
        node_lat, node_lon = element.get("lat"), element.get("lon")
        if not name or node_lat is None or node_lon is None:
            continue
        distance = _distance_m(lat, lon, node_lat, node_lon)
        if best_node is None or distance < best_node[1]:
            best_node = (name, distance)

    if best_node is not None:
        return best_node[0], True
    return None, True


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
        # A nearby islet's own name always beats whatever Nominatim's plain reverse lookup
        # happened to match instead -- see _nearby_islet_name.
        islet_name, islet_check_ok = _nearby_islet_name(lat, lon, self.user_agent)
        self._last_request = time.monotonic()
        if islet_name:
            return islet_name, True
        place = _describe_place(payload, lat, lon)
        # Not cacheable if the islet check itself failed (as opposed to completing and simply
        # finding nothing nearby): otherwise this run's plain Nominatim fallback -- possibly the
        # wrong name, that's the whole reason the check exists -- would get permanently stuck in
        # the cache even once Overpass is reachable again on a later run.
        return place, islet_check_ok

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
    address = payload.get("address", {})
    is_leisure_match = payload.get("category") == "leisure" and payload.get("name")

    # A leisure match mapped as a bare point node (no polygon/area of its own) is often a small,
    # specific sub-feature -- e.g. one named quay -- rather than the harbour a boat is really
    # moored at, so a real village/town/city name is trusted over it instead (found in practice:
    # "Darse de Castéro", a single OSM node, replacing the correct and far more recognizable
    # "Port Haliguen" village name). A way/relation match (an actually mapped area, e.g. a real
    # marina basin) is trusted as before -- unlike a bare node, its shape is real evidence the
    # boat is genuinely inside it, not just near a named point (found in practice: Port Olona,
    # Port de Plaisance de Pornichet, Port du Crouesty are all real mapped areas and keep their
    # own name here, unaffected).
    if is_leisure_match and payload.get("osm_type") == "node":
        for key in _VILLAGE_LEVEL_ADDRESS_KEYS:
            if key in address:
                return address[key]

    if is_leisure_match:
        return payload["name"]

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
