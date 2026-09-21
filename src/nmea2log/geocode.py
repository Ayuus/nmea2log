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

from ._net import FailureBreaker, urlopen_ipv4_first
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


_LANDMARK_MAX_RETRIES = 2


# Same reasoning as _LANDMARK_MAX_RETRIES below, applied to the main Nominatim reverse-geocode
# request itself -- found in practice, a real, reproducible-in-isolation flake: the exact same
# coordinate that failed with a dropped connection mid-run resolved correctly on every one of 4
# immediate, separate retries moments later. A single failure here used to be final (no retry at
# all, unlike the Overpass islet check just below), so one bad moment on Nominatim's end
# permanently shows "geocoding failed" for that place in this run's own output, even though a
# request a second later would have worked fine.
_NOMINATIM_MAX_RETRIES = 2

# Address levels that count as "a village name" -- used by _pick_place_name (a real village/
# town/city beats an obscure leisure match, see there) and shared here so anything else that
# needs the same definition of "a real village/town/city" can reuse it.
_VILLAGE_LEVEL_ADDRESS_KEYS = ("town", "village", "city", "municipality", "suburb", "quarter")

# Below this, a leisure match's own Nominatim "importance" score counts as obscure enough that a
# real village/town/city name is preferred instead (see _pick_place_name). Chosen from real data,
# not tuned to a single case: every leisure match seen in practice fell cleanly into one of two
# widely separated groups -- ~0.00005-0.0001 (obscure) or ~0.17-0.19 (well-known) -- so this only
# needs to sit somewhere in the three-orders-of-magnitude gap between them, not on a fine line.
_OBSCURE_IMPORTANCE_THRESHOLD = 0.001


def _nearby_landmark_name(lat: float, lon: float, user_agent: str) -> Tuple[Optional[str], bool]:
    """Looks for the nearest named islet or lock within _LANDMARK_SEARCH_RADIUS_M via Overpass,
    since Nominatim's own reverse geocode doesn't reliably recognize either -- it matches
    whatever unrelated feature happens to be nearest by its own metric instead (found in
    practice: anchored a couple hundred meters off a named islet, Nominatim matched an unrelated
    pier instead, using a real but different nearby hamlet; a lock complex came back as a plain
    village name, with the actual lock -- tagged lock=yes/lock_name -- entirely ignored).
    A separate service (not Nominatim) since Nominatim's own search endpoint can't filter by OSM
    tag, only match free-text against a name we'd have to already know.

    Marinas and bridges were both tried here and dropped again (asked for explicitly, checked
    against every distinct point in a real, full-season logbook before deciding): marinas just
    duplicate work Nominatim's own address usually already does, and a marina genuinely missing
    from it is meant to be fixed by improving OpenStreetMap's own data for that spot, not worked
    around here; bridges matched real but unhelpful small pedestrian/pontoon footbridges within
    an already-well-named harbour (e.g. "Port-Louis" -> "Epices", a bridge inside that harbour)
    more often than they matched anything worth surfacing. Locks alone, checked the same way,
    changed exactly one point in that same real logbook -- the one it was meant to fix -- with no
    unwanted side effects anywhere else: French waterway locks in particular are commonly tagged
    with their own lock_name (e.g. "Écluse du barrage d'Arzal") that Nominatim's plain address
    lookup has no equivalent field for at all, regardless of how well-mapped the lock itself is.

    Only a plain name *node* is used for islets, never the coastline *way* that usually also
    exists for the same islet -- found in practice: anchored well within a pier's own address
    match (38 m away, genuinely at that harbour), yet a large islet's coastline way still had
    *some* point of its shape within the search radius, so its own "center" (the way's geometric
    centroid, not the nearest point of its actual coastline) ended up nearly 680 m away and
    wrongly won outright over the harbour the boat was actually moored at. A lock is the opposite
    case -- it's near-always mapped as a way (the lock chamber itself), so its own "center" is
    used for that, just not for islets.

    Returns ``(name, ok)`` -- ``ok`` is False only if the check itself never completed (every
    retry failed), as opposed to completing and simply finding nothing nearby, so the caller can
    tell the two apart: a confirmed "nothing here" is safe to cache, a failed check is not (the
    caller should retry it on a later run instead of being stuck with today's plain Nominatim
    fallback forever -- see Geocoder._lookup)."""
    # [out:json] is required -- without it Overpass answers with its own default format (XML,
    # HTTP 200) instead of an error, so a missing/dropped [out:json] fails json.loads on every
    # single request instead of just occasionally under load (found in practice: silently
    # reintroduced while simplifying this query, turned every real run's landmark check into a
    # guaranteed failure until the retries gave up).
    #
    # "out tags;" alone omits geometry entirely -- for a node, no lat/lon at all, not even its
    # own position (found live: a real, correctly-matched "Île de la Jument" node came back with
    # only its id and tags, nothing else, so this treated it as if no islet existed here at all
    # and cached that as the confirmed answer); for a way, there's no single position at all
    # without it. "center" adds both back -- a node's own coordinates unchanged, a way's own
    # computed centroid.
    around = f"around:{_LANDMARK_SEARCH_RADIUS_M:.0f},{lat:.6f},{lon:.6f}"
    query = (
        f"[out:json][timeout:10];"
        f'(node({around})["place"="islet"]["name"];'
        f'way({around})["lock"="yes"]["lock_name"];'
        f'node({around})["lock"="yes"]["lock_name"];'
        f");"
        "out tags center;"
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
            with urlopen_ipv4_first(request, timeout=15) as response:
                status = response.status
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("remark"):
                # Overpass answers HTTP 200 with valid, parseable JSON even when the query
                # itself timed out server-side and only partially ran -- a "remark" key is how
                # it signals that (found in practice: under load, an empty "elements" list from
                # a timed-out query looked exactly like a confirmed "nothing here", and got
                # cached as that permanently instead of being retried).
                remark = payload["remark"]
                payload = None
                raise ValueError(remark)
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
                f"[geocode] Overpass landmark check failed ({exc}) [http {status}] "
                f"-- attempt {attempt + 1}/{_LANDMARK_MAX_RETRIES + 1}",
                file=sys.stderr,
            )
    if payload is None:
        return None, False  # best-effort: caller falls back to the plain Nominatim result

    # is_mooring picks which of _describe_place()'s two "you're not quite there" prefixes a
    # far-enough-away match gets, same idea as _MOORING_TYPES below: a lock is somewhere a boat
    # actually ties up alongside while waiting (like a marina/quay), an islet is not (you anchor
    # off it, not on it) -- see _with_distance_prefix()'s own doc comment.
    best: Optional[Tuple[str, float, bool]] = None
    for element in payload.get("elements", []):
        tags = element.get("tags", {})
        if tags.get("place") == "islet":
            # A node's own lat/lon only, never a way's "center" -- see this function's own doc
            # comment on why a coastline way's centroid can't be trusted as "how close is this
            # islet really" the way a lock's own centroid can.
            name = tags.get("name")
            node_lat, node_lon = element.get("lat"), element.get("lon")
            is_mooring = False
        elif tags.get("lock") == "yes":
            # A node's own lat/lon directly; a way's own computed centroid instead (locks are
            # near-always mapped as ways, the lock chamber itself).
            name = tags.get("lock_name")
            node_lat = element.get("lat")
            node_lon = element.get("lon")
            if node_lat is None:
                center = element.get("center") or {}
                node_lat, node_lon = center.get("lat"), center.get("lon")
            is_mooring = True
        else:
            continue
        if not name or node_lat is None or node_lon is None:
            continue
        distance = _distance_m(lat, lon, node_lat, node_lon)
        if best is None or distance < best[1]:
            best = (name, distance, is_mooring)

    if best is not None:
        name, distance, is_mooring = best
        # Found in practice, the reason _LANDMARK_SEARCH_RADIUS_M (300 m) is deliberately wider
        # than _NEARBY_THRESHOLD_M (250 m) used here: anchored 268 m off Île de la Jument, close
        # enough to be the obvious match and still worth surfacing, but far enough that showing
        # its bare name implied being right there -- same "aan de kant, bij"/"op het water, bij"
        # treatment a plain Nominatim match already gets (see _describe_place()), previously
        # missing here entirely since a landmark match used to always return its bare name.
        return _with_distance_prefix(name, distance, is_mooring), True
    return None, True


class Geocoder:
    def __init__(
        self,
        *,
        cache_file: Optional[Path] = None,
        user_agent: str = "nmea2log/0.1 (personal sailing logbook)",
        # Empty (default) means "don't ask for a specific language at all" -- see _lookup(),
        # which then omits Nominatim's accept-language parameter entirely rather than sending an
        # empty value for it. Nominatim's own documented behavior with no accept-language given is
        # to return each place's plain, untranslated OSM "name" tag -- i.e. already whatever
        # language that place is actually named in locally (a French harbour comes back in
        # French, a Dutch one in Dutch), not a fixed language picked ahead of time. Explicitly
        # requesting a language (e.g. "nl") instead asks Nominatim to prefer that place's
        # name:<lang> tag when one exists, which for a well-known feature that happens to have a
        # translated tag (found in practice: some larger bodies of water do) can return a
        # translated name even for a place whose real, spoken-there name is something else.
        language: str = "",
        precision: int = 3,
    ) -> None:
        self.cache_file = cache_file
        self.user_agent = user_agent
        self.language = language
        self.precision = precision
        self._cache: dict[str, str] = {}
        self._last_request = 0.0
        # See FailureBreaker: what happens to the places once a service has been given up on for this run.
        self._nominatim_breaker = FailureBreaker(
            "Nominatim", "geocode",
            "The places after this show only their coordinates and are not cached, so a later run looks them up again.",
        )
        self._landmark_breaker = FailureBreaker(
            "Overpass landmark check", "geocode",
            "The places after this keep Nominatim's own name and are not cached, so a later run tries the check again.",
        )
        if cache_file is not None and cache_file.exists():
            self._cache = self._migrate_cache_precision(json.loads(cache_file.read_text(encoding="utf-8")))

    def _migrate_cache_precision(self, raw_cache: dict[str, str]) -> dict[str, str]:
        """Re-keys every entry in an already-loaded cache to this instance's own current
        ``precision`` -- found in practice, a real bug: changing the default precision (see
        ``_key``'s own doc comment) left every entry already on disk keyed at the *old*
        precision, so none of them ever matched a freshly-computed key again -- every single
        lookup missed the cache and re-hit Nominatim/Overpass for a place that had, in fact,
        already been looked up before. Re-keying (not just re-rounding the string -- the key
        itself already lost precision once, so this parses the coordinates back out and rounds
        them again at the current precision) makes every already-known place reachable again
        under the key ``_key`` would compute for it today. Collisions (two old keys now rounding
        to the same new one) just keep whichever entry happens to be seen last -- harmless, since
        they already named the same real-world place closely enough to share a bucket.
        Re-saved immediately below if anything actually changed, so this migration only runs
        once, not on every subsequent load."""
        migrated: dict[str, str] = {}
        changed = False
        for key, value in raw_cache.items():
            try:
                lat_str, lon_str = key.split(",")
                new_key = self._key(float(lat_str), float(lon_str))
            except ValueError:
                continue  # a malformed/unexpected key -- drop it rather than crash the whole load
            migrated[new_key] = value
            if new_key != key:
                changed = True
        if changed and self.cache_file is not None:
            self.cache_file.write_text(json.dumps(migrated, ensure_ascii=False, indent=2), encoding="utf-8")
        return migrated

    def _key(self, lat: float, lon: float) -> str:
        # precision=3 (~110 m at this latitude), not 4 (~11 m) -- found in practice, on a real,
        # growing cache file: the same real-world stay's own averaged position (see
        # tripbuilder.py's Stay construction) isn't perfectly deterministic run to run -- slightly
        # different sample grouping/cache hits shift it by a few metres, easily enough to land in
        # a different 4-decimal bucket and silently miss an already-cached place name every time,
        # defeating the whole point of caching it (several near-duplicate keys for the exact same
        # harbour, a few metres apart, were found sitting side by side in one real cache file).
        # ~110 m is loose enough to absorb that drift while still comfortably narrower than the
        # distance between two genuinely different, separately-named real-world moorings.
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

        request_params = {
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
        }
        if self.language:
            request_params["accept-language"] = self.language
        # else: deliberately omitted, not sent as an empty string -- see this class's own
        # ``language`` doc comment for why that gets Nominatim's own local-name default instead.
        params = urllib.parse.urlencode(request_params)
        request = urllib.request.Request(
            f"{_NOMINATIM_URL}?{params}", headers={"User-Agent": self.user_agent}
        )
        if self._nominatim_breaker.off:
            return f"Onbekend ({lat:.4f}, {lon:.4f})", False
        payload = None
        last_exc: Optional[Exception] = None
        for attempt in range(_NOMINATIM_MAX_RETRIES + 1):
            if attempt:
                time.sleep(2.0)
            try:
                with urlopen_ipv4_first(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            # OSError alongside URLError: some connection failures (e.g. the server dropping the
            # connection mid-response) surface as a raw ConnectionResetError/http.client
            # exception, not wrapped in URLError (found in practice: http.client.RemoteDisconnected
            # crashed the whole run instead of being treated as a normal, non-cacheable lookup
            # failure).
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self._last_request = time.monotonic()
                last_exc = exc
                log(
                    f"[geocode] Nominatim lookup failed ({exc}) -- attempt "
                    f"{attempt + 1}/{_NOMINATIM_MAX_RETRIES + 1}",
                    file=sys.stderr,
                )
        self._nominatim_breaker.record(payload is not None)
        if payload is None:
            # Just "Onbekend (lat, lon)", not the full exception text -- asked for explicitly:
            # the raw urllib error (a whole "<urlopen error [WinError 10054] ...>" sentence) read
            # as noise in the logbook itself, which isn't the place for that level of technical
            # detail -- the retried attempts above (and this failure) are still visible in
            # nmea2log.log for anyone who wants it.
            return f"Onbekend ({lat:.4f}, {lon:.4f})", False

        self._last_request = time.monotonic()
        # A nearby islet/lock/bridge's own name always beats whatever Nominatim's plain reverse
        # lookup happened to match instead -- see _nearby_landmark_name.
        landmark_name, landmark_check_ok = self._landmark_check(lat, lon)
        self._last_request = time.monotonic()
        if landmark_name:
            return landmark_name, True
        place = _describe_place(payload, lat, lon)
        # Not cacheable if the landmark check itself failed (as opposed to completing and simply
        # finding nothing nearby): otherwise this run's plain Nominatim fallback -- possibly the
        # wrong name, that's the whole reason the check exists -- would get permanently stuck in
        # the cache even once Overpass is reachable again on a later run.
        return place, landmark_check_ok

    def _landmark_check(self, lat: float, lon: float) -> Tuple[Optional[str], bool]:
        """_nearby_landmark_name, unless Overpass has been given up on for this run: then no request at all,
        and ``ok`` False like for a failed check."""
        if self._landmark_breaker.off:
            return None, False
        name, ok = _nearby_landmark_name(lat, lon, self.user_agent)
        self._landmark_breaker.record(ok)
        return name, ok

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

    # A leisure match Nominatim itself considers obscure (a low "importance" score, its own
    # measure of how well-known a place is) is often a small, specific sub-feature -- e.g. one
    # named quay -- rather than the harbour a boat is really moored at, so a real village/town/
    # city name is trusted over it instead (found in practice: "Darse de Castéro" and "Port de
    # Plaisance de Pornichet", both importance < 0.0001, replacing the correct and far more
    # recognizable "Port Haliguen"/"Pornichet"). A well-known match is trusted as before --
    # Port Olona and Port du Crouesty both score two orders of magnitude higher (~0.17-0.19) and
    # keep their own name here, unaffected, same as any match Nominatim doesn't consider obscure
    # at all (e.g. Concarneau, whose marina and village name happen to be identical anyway).
    # Defaults to 1.0 (i.e. not obscure) when the field is missing or null -- some real Nominatim
    # responses omit it -- so an absent score never silently triggers the village swap on its own.
    importance = payload.get("importance")
    if importance is None:
        importance = 1.0
    if is_leisure_match and importance < _OBSCURE_IMPORTANCE_THRESHOLD:
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

    return f"Onbekend ({lat:.4f}, {lon:.4f})"


# Module-level (not just local to _with_distance_prefix) so html_writer.py can import the exact
# same literals to split a formatted place name back apart into (name, prefix_kind) for the HTML
# logbook's own language switcher (see html_writer.py's _split_place_prefix()) -- these strings
# have to stay in lockstep with each other by construction, not by two call sites happening to
# agree on the same text.
PREFIX_MOORING = "aan de kant, bij"
PREFIX_WATER = "op het water, bij"


def _with_distance_prefix(name: str, distance_m: float, is_mooring_type: bool) -> str:
    """Prefixes name with PREFIX_MOORING (alongside, near) or PREFIX_WATER (on the water, near)
    when distance_m is more than ``_NEARBY_THRESHOLD_M`` -- otherwise the name reads as if we were
    right there, e.g. showing a village name for a position that was really anchored ~250 m
    offshore of it. is_mooring_type picks which of the two prefixes: alongside for somewhere a
    boat actually ties up (a marina, a lock waiting alongside it, ...), on the water for
    everything else (open water, an islet you'd anchor off rather than on, ...)."""
    if distance_m <= _NEARBY_THRESHOLD_M:
        return name
    prefix = PREFIX_MOORING if is_mooring_type else PREFIX_WATER
    return f"{prefix} {name}"


def _describe_place(payload: dict, lat: float, lon: float) -> str:
    name = _pick_place_name(payload, lat, lon)
    try:
        feature_lat = float(payload["lat"])
        feature_lon = float(payload["lon"])
    except (KeyError, TypeError, ValueError):
        return name
    distance = _distance_m(lat, lon, feature_lat, feature_lon)
    return _with_distance_prefix(name, distance, payload.get("type") in _MOORING_TYPES)
