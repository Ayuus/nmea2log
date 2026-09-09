"""Turns sequences of positions/speeds/engine data into logbook trips (departure port ->
arrival port).

Approach: every GPS fix is classified as 'stationary' or 'underway' based on speed over
ground. Consecutive stationary periods that last long enough (threshold ``min_stop_minutes``)
are considered a port visit; the periods in between are the trips. For each trip, fuel
consumption is calculated by integrating the fuel-rate readings (PGN 127489, engine data) over
time -- so explicitly not via a tank sensor.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import groupby
from typing import Dict, FrozenSet, List, Optional, Tuple

from .fix_array import AttitudeArray, FixArray, SogArray
from .geocode import Geocoder, NoGeocoder
from .log import log
from .model import (
    AttitudeSample,
    BatterySample,
    DepthSample,
    EngineRpmSample,
    EngineSample,
    PositionFix,
    SogSample,
    TripFuelSample,
    WaterTempSample,
)

_KNOT_IN_MS = 0.514444
_EARTH_RADIUS_NM = 3440.065
_MAX_INTEGRATION_GAP_H = 1.0  # larger gaps between engine samples indicate a log interruption
_KELVIN_TO_CELSIUS = 273.15
_PA_TO_BAR = 1e-5
_ENGINE_IDLE_FUEL_LPH = 0.3  # below this, the engine counts as switched off rather than idling
_ENGINE_OFF_GAP_S = 60.0  # a gap this long between "on" readings means the engine was actually
# switched off in between, not just a brief hiccup in PGN reporting
_RPM_BUCKET = 50  # round RPM to the nearest multiple of this before taking the mode

# Bump this whenever a change to build_trips()'s own logic could change what an already-cached
# ("settled") trip looks like -- a different track/position computation, a different fold/merge
# decision, etc. -- even though none of build_trips()'s own *parameters* changed. Found in
# practice: the trip cache (see trip_cache.py's config_signature()) only compares those explicit
# parameters, so a pure logic fix (e.g. the arrival/departure track-to-marker splice) silently
# kept serving already-settled trips built under the old logic, with no visible effect on synced
# data until the affected trips aged out of the cache on their own -- on a real device, that's
# potentially never. Included in config_signature() specifically so a bump here always forces a
# one-time full rebuild instead.
TRIP_LOGIC_VERSION = 2
_RPM_STABLE_MINUTES = 2.0  # a run at the typical RPM bucket must last at least this long to
# count as steady cruising rather than a brief pass-through while accelerating/decelerating


@dataclass(frozen=True, slots=True)
class NavSample:
    """slots=True matters here specifically (unlike Stay/EngineHealth/BatteryHealth/TripLeg
    below, all per-trip and so never more than a few dozen instances) -- one NavSample gets
    built per merged GPS fix (see _merge_nav_samples), so a real multi-year archive holds
    millions of these at once, for the whole time build_trips() runs. Same reasoning, and same
    ~38% measured reduction, as model.py's own slots=True note on PositionFix/SogSample -- this
    is what those get turned into internally, and (now that fixes/sogs themselves are stored far
    more compactly, see fix_array.py) is the single biggest season-wide Python-object cost left."""

    time: datetime
    lat: float
    lon: float
    sog_ms: float
    depth_m: Optional[float] = None
    water_temp_c: Optional[float] = None
    cog_deg: Optional[float] = None


@dataclass(frozen=True, slots=True)
class Stay:
    start: datetime
    end: datetime
    lat: float
    lon: float
    place: str


@dataclass(frozen=True, slots=True)
class EngineHealth:
    oil_pressure_bar_avg: Optional[float]
    oil_temperature_c_avg: Optional[float]
    coolant_temperature_c_avg: Optional[float]
    alternator_voltage_v_avg: Optional[float]
    engine_load_pct_max: Optional[float]
    warnings: FrozenSet[str]
    warning_first_seen: Dict[str, datetime] = field(default_factory=dict)  # warning text -> first time seen


@dataclass(frozen=True, slots=True)
class BatteryHealth:
    avg_voltage_v: Optional[float]
    min_voltage_v: Optional[float]
    min_voltage_at: Optional[datetime] = None  # moment the min_voltage_v reading was recorded


@dataclass(frozen=True, slots=True)
class TripLeg:
    depart_time: datetime
    arrive_time: datetime
    depart_place: str
    arrive_place: str
    # The averaged position of the stay this trip departed from/arrived at (mean of every
    # stationary GPS fix during that stay, same value the place name itself was looked up from --
    # see Stay/build_trips), not a single fix from the moment the boat started/stopped moving.
    # A lone fix has real GPS jitter (found in practice: ~10 m off from the actual berth) that
    # averaging over the whole stay cancels out; falls back to the trip's own first/last fix only
    # when there's no stay at all (started/ended outside the log file).
    depart_lat: float
    depart_lon: float
    arrive_lat: float
    arrive_lon: float
    duration: timedelta  # time underway, excluding any gaps in the data (see _moving_duration)
    distance_nm: float
    avg_speed_kn: Optional[float]
    max_speed_kn: Optional[float]
    fuel_liters: float  # calculated by integrating the fuel rate (PGN 127489) over time
    fuel_liters_device: Optional[float]  # engine's own trip meter (PGN 127497), None = not available
    # engine instance -> hours run during this trip, extended by however long that engine ran
    # continuously right before departure and after arrival (see _extend_engine_window)
    engine_hours: Dict[int, float]
    engine_hours_total: Dict[int, float]  # engine instance -> absolute hour-meter reading at arrival
    engine_health: Dict[int, EngineHealth]  # engine instance -> health indicators + warnings
    typical_rpm: Dict[int, float]  # engine instance -> most commonly occurring RPM during the trip
    # engine instance -> (min, max, avg speed in kn, avg fuel in L/nm or None) while holding that RPM
    typical_rpm_speed_kn: Dict[int, Tuple[float, float, float, Optional[float]]]
    battery_health: Dict[int, BatteryHealth]  # battery instance -> voltage stats during this trip
    min_depth_m: Optional[float]  # shallowest water depth measured during this trip
    min_depth_lat: Optional[float]
    min_depth_lon: Optional[float]
    avg_water_temp_c: Optional[float]
    min_water_temp_c: Optional[float]
    max_water_temp_c: Optional[float]
    roll_variation_deg: Optional[float]  # standard deviation of roll (PGN 127257) during the trip
    pitch_variation_deg: Optional[float]  # standard deviation of pitch (PGN 127257) during the trip
    roll_range_deg: Optional[float]  # peak-to-peak (max - min) roll during the trip
    pitch_range_deg: Optional[float]  # peak-to-peak (max - min) pitch during the trip
    track: List[NavSample]  # GPS points of this trip, e.g. for GPX export
    max_speed_at: Optional[datetime] = None  # moment the max speed (see max_speed_kn) was recorded
    max_speed_rpm: Dict[int, float] = field(default_factory=dict)  # engine instance -> RPM at that moment


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_NM * math.asin(math.sqrt(a))


_MAX_PLAUSIBLE_SPEED_KN = 60.0  # generous margin above the fastest speed this app has ever
# recorded (~22 kn) -- exists purely to catch corrupted position fixes, not to model anything
# about the boat itself.


def _reject_gps_outliers(fixes: List[PositionFix]) -> List[PositionFix]:
    """Drops a position fix that implies an impossible speed from the last *accepted* fix.

    Confirmed in practice, byte-for-byte: a real .ebl file contained a well-formed record (valid
    length, valid CAN ID for PGN 129025 on a real GPS source) whose 8-byte position payload was
    the correct value with its first 2 bytes moved to the end -- i.e. genuinely corrupted data
    already sitting in the raw file, not a bug in how this file is parsed (checked against the
    reference Go implementation this parser is based on, and its issues/PRs -- nothing similar
    reported there either). One such fix reported a position ~7800 nm away for a single sample,
    which balloons a trip's reported distance by thousands of nm even though the boat never
    actually went there. Only ever seen on one of the boat's two GPS sources so far, and not
    fixable by simply preferring the other source everywhere: that source has entire days with no
    data at all where the affected one does (found in practice) -- dropping a corrupted fix costs
    far less than dropping whole trips.

    Comparing against the last *accepted* fix, not simply the previous one in the list, is what
    lets a single bad fix get dropped without also rejecting the good fix right after it."""
    if not fixes:
        return fixes
    sorted_fixes = sorted(fixes, key=lambda f: f.time)
    accepted = [sorted_fixes[0]]
    for fix in sorted_fixes[1:]:
        prev = accepted[-1]
        dt_hours = (fix.time - prev.time).total_seconds() / 3600
        if dt_hours <= 0:
            continue  # duplicate/out-of-order timestamp -- keep whichever came first
        implied_speed_kn = _haversine_nm(prev.lat, prev.lon, fix.lat, fix.lon) / dt_hours
        if implied_speed_kn > _MAX_PLAUSIBLE_SPEED_KN:
            continue
        accepted.append(fix)
    return accepted


def _reject_gps_outliers_array(fixes: FixArray) -> FixArray:
    """Same algorithm as _reject_gps_outliers() above, operating on a FixArray instead of a
    List[PositionFix] -- used by _merge_nav_samples() for the real season-wide data, so a run
    never has to materialize a full PositionFix object per fix just to filter them. Kept as a
    separate function rather than making _reject_gps_outliers() itself generic: that one has its
    own direct unit test asserting an exact List[PositionFix] in, List[PositionFix] out contract,
    and duplicating ~15 lines here is a lot cheaper than risking that carefully-tuned, real-bug-
    fixing logic (see its own docstring) on a rewrite. See test_reject_gps_outliers_array_* for
    this version's own coverage of the same real-world corrupted-fix scenario."""
    n = len(fixes)
    if n == 0:
        return fixes
    order = sorted(range(n), key=fixes.time_at)
    accepted = FixArray()
    prev_i = order[0]
    accepted.append_raw(fixes.time_at(prev_i), fixes.lat_at(prev_i), fixes.lon_at(prev_i))
    for i in order[1:]:
        dt_hours = (fixes.time_at(i) - fixes.time_at(prev_i)) / 3600
        if dt_hours <= 0:
            continue  # duplicate/out-of-order timestamp -- keep whichever came first
        implied_speed_kn = (
            _haversine_nm(fixes.lat_at(prev_i), fixes.lon_at(prev_i), fixes.lat_at(i), fixes.lon_at(i))
            / dt_hours
        )
        if implied_speed_kn > _MAX_PLAUSIBLE_SPEED_KN:
            continue
        accepted.append_raw(fixes.time_at(i), fixes.lat_at(i), fixes.lon_at(i))
        prev_i = i
    return accepted


def _merge_nav_samples(
    fixes: FixArray,
    sogs: SogArray,
    depths: Optional[List[DepthSample]] = None,
    water_temps: Optional[List[WaterTempSample]] = None,
) -> List[NavSample]:
    """Combines position, speed, depth, and water temperature readings chronologically; all are
    forward-filled.

    fixes/sogs are FixArray/SogArray (see fix_array.py) rather than plain lists -- always, by the
    time this is called from build_trips() (the only caller), which wraps whatever it was given
    into those types up front. A season's worth of either can be millions of samples, and this is
    the one place a PositionFix/SogSample object gets constructed per fix at all (NavSample below
    is what the rest of the algorithm actually works with from here on). SOG in particular used
    to go through a plain sorted(sogs, key=lambda s: s.time) here, materializing a full season's
    worth of SogSample objects that then stayed resident for this whole function's run -- found
    in practice, on a real ~2 million-fix archive, this function is where the phone's decode-
    through-publish pipeline was actually dying (confirmed via the checkpoint logging around
    build_trips(), see that function's own comment): SOG is typically similar cardinality to
    position fixes, so that materialization alone was a real, multi-hundred-MB cost on top of
    everything else already resident at that point."""
    fixes = _reject_gps_outliers_array(fixes)
    # Already in time order -- _reject_gps_outliers_array() processes its input in sorted order
    # and never reorders what it keeps, so no second sort is needed here (the original
    # list-based version technically re-sorted an already-sorted list every single run).
    sogs = sogs.sorted_by_time()
    depths_sorted = sorted(depths, key=lambda s: s.time) if depths else []
    water_temps_sorted = sorted(water_temps, key=lambda s: s.time) if water_temps else []
    samples: List[NavSample] = []
    sog_idx = 0
    depth_idx = 0
    water_temp_idx = 0
    last_sog = 0.0
    last_cog: Optional[float] = None
    last_depth: Optional[float] = None
    last_water_temp: Optional[float] = None
    for i in range(len(fixes)):
        fix_time = fixes.datetime_at(i)
        fix_epoch = fixes.time_at(i)
        while sog_idx < len(sogs) and sogs.time_at(sog_idx) <= fix_epoch:
            last_sog = sogs.sog_at(sog_idx)
            last_cog = sogs.cog_at(sog_idx)
            sog_idx += 1
        while depth_idx < len(depths_sorted) and depths_sorted[depth_idx].time <= fix_time:
            last_depth = depths_sorted[depth_idx].depth_m
            depth_idx += 1
        while water_temp_idx < len(water_temps_sorted) and water_temps_sorted[water_temp_idx].time <= fix_time:
            last_water_temp = water_temps_sorted[water_temp_idx].temp_c
            water_temp_idx += 1
        samples.append(
            NavSample(fix_time, fixes.lat_at(i), fixes.lon_at(i), last_sog, last_depth, last_water_temp, last_cog)
        )
    return samples


def _classify_runs(
    samples: List[NavSample], speed_threshold_ms: float
) -> List[Tuple[str, List[NavSample]]]:
    labelled = [("stationary" if s.sog_ms < speed_threshold_ms else "moving", s) for s in samples]
    return [(label, [s for _, s in group]) for label, group in groupby(labelled, key=lambda t: t[0])]


def _merge_short_stops(
    runs: List[Tuple[str, List[NavSample]]], min_stop: timedelta, max_gap: timedelta
) -> List[Tuple[str, List[NavSample]]]:
    """Stationary periods shorter than the threshold normally don't count as a port visit and
    get folded into the trip -- EXCEPT when the period is adjacent to a data gap of at least
    ``max_gap``: the measured duration is then artificially short (cut off by the gap, not
    because the boat really only stopped briefly), so it still counts as a port visit. Without
    this exception, a trip right before such a gap would lose its "arrival port" (found in
    practice: a trip that ended at a real, confirmed mooring still got "Unknown" as its arrival
    port, purely because there were only a few minutes of data after arrival before the gap
    started)."""
    relabelled = []
    for idx, (label, group) in enumerate(runs):
        if label == "stationary" and (group[-1].time - group[0].time) < min_stop:
            gap_before = idx > 0 and (group[0].time - runs[idx - 1][1][-1].time) >= max_gap
            gap_after = idx + 1 < len(runs) and (runs[idx + 1][1][0].time - group[-1].time) >= max_gap
            if not (gap_before or gap_after):
                label = "moving"
        relabelled.append((label, group))

    return _merge_adjacent(relabelled)


def _merge_adjacent(runs: List[Tuple[str, List[NavSample]]]) -> List[Tuple[str, List[NavSample]]]:
    """Joins consecutive runs that ended up with the same label after relabelling (e.g. a
    "stationary" run just turned back into "moving") into a single run."""
    merged: List[Tuple[str, List[NavSample]]] = []
    for label, group in runs:
        if merged and merged[-1][0] == label:
            merged[-1] = (label, merged[-1][1] + group)
        else:
            merged.append((label, group))
    return merged


def _spatial_spread_m(group: List[NavSample]) -> float:
    """Max distance (meters) from the group's centroid to any point in it."""
    lat = sum(s.lat for s in group) / len(group)
    lon = sum(s.lon for s in group) / len(group)
    return max(_haversine_nm(lat, lon, s.lat, s.lon) * 1852.0 for s in group)


def _settled_position(
    group: List[NavSample],
    speed_threshold_ms: float,
    on_intervals: List[Tuple[datetime, datetime]],
    lock_radius_m: Optional[float] = None,
) -> Tuple[float, float]:
    """Averaged (lat, lon) for a stationary stay, using only its own genuinely-at-rest samples --
    excludes anything under half ``speed_threshold_ms`` still counts as gliding, and anything
    the engine was still confirmed running for -- rather than every sample in the group.

    Found in practice, on real data: a boat gliding the last few metres into a berth crosses
    below ``speed_threshold_ms`` (so those samples already count as "stationary") before it's
    actually stopped moving -- e.g. a real arrival whose last "moving" sample was still doing
    0.5 kn, right at the default threshold, meaning the *next* few samples right after are still
    likely doing 0.4, 0.3, 0.2 kn while continuing to glide those last few metres. Averaging the
    whole stay from its very first sample pulled the reported arrival position back along that
    approach path, a real (if usually small, a few metres) offset from the boat's actual final
    position, showing up as a visible gap between the drawn track's own last point and the
    arrival marker on the map.

    The engine-on exclusion (asked for explicitly) catches the same kind of not-really-at-rest-
    yet sample a different way: while the engine's still running, the skipper may still be
    actively working the boat into its final spot (bow thruster nudges, brief reversing, ...)
    even at a moment SOG itself already reads near zero, which the speed filter alone can't see.

    Half the classification threshold, not some much stricter fixed value: strict enough to
    exclude a still-gliding sample sitting close to the classification boundary, loose enough
    that ordinary GPS jitter on a genuinely stationary boat (which real data shows rarely
    reads as literally 0.0 kn) doesn't leave nothing qualifying. Each filter falls back to the
    next-loosest result (speed-and-engine, then speed-only, then the whole group) rather than
    ever computing an average over zero samples -- e.g. an unusually short or noisy stay, or one
    where the engine happens to still be running for its entire recorded duration.

    First narrowed to samples after the *last* engine restart within the group's own span, if
    any -- found in practice, on real data: a boat that engine-off'd briefly at an intermediate
    stop (e.g. floating near a waypoint while waiting) before a short engine-on transit to its
    actual final berth had both stops folded into one continuous "stay" (a short in-transit leg
    between them, folded by _merge_negligible_trips, also merges the stationary run on either
    side of it -- see that function's own stationary-merge branch). Every sample from the
    intermediate stop was just as much "engine off" as the real final berth, so the plain
    engine-on exclusion above couldn't tell them apart -- averaging over both diluted the
    reported arrival position to roughly halfway between two genuinely different places,
    regardless of how briefly the boat was actually at the first one (confirmed on real data: a
    ~600-sample intermediate stop and a similarly-sized final one landed the reported position
    about 70 m from the boat's actual, confirmed berth). Only the time after the boat's *last*
    engine shutdown is its own actual final resting stretch; anything before that belongs to an
    earlier, already-departed-from sub-stop within the same merged stay.

    Only actually narrows when the position before vs. after that restart differs by more than
    ``lock_radius_m`` -- found in practice, on real data, right after the fix above: a stay whose
    engine cycled back on for a few minutes (e.g. running a generator/charging batteries while
    already moored) but whose position barely changed (~8 m) got needlessly narrowed from over a
    thousand samples down to a dozen, trading a robust average for a noisy one to "fix" a gap
    that was never really there -- the earlier fix's own reasoning (two genuinely different
    places) simply didn't apply when the boat never actually went anywhere. Reuses
    ``lock_radius_m`` for the same reason ``_merge_negligible_trips`` does (see its own
    docstring): both are really asking the same question, "did the boat actually leave about this
    radius". Skips the narrowing entirely (falls back to the whole group, today's -- pre-this-
    fix's -- behavior) when ``lock_radius_m`` is ``None``, same as lock/bridge detection itself
    being off: without a confinement radius to judge "did it really move" against, guessing
    either way risks being wrong, so this stays conservative and leaves the whole group alone."""
    last_restart_end = max(
        (end for start, end in on_intervals if group[0].time <= start <= group[-1].time),
        default=None,
    )
    if last_restart_end is not None and lock_radius_m is not None:
        after_restart = [s for s in group if s.time >= last_restart_end]
        before_restart = [s for s in group if s.time < last_restart_end]
        if after_restart and before_restart:
            before_lat = sum(s.lat for s in before_restart) / len(before_restart)
            before_lon = sum(s.lon for s in before_restart) / len(before_restart)
            after_lat = sum(s.lat for s in after_restart) / len(after_restart)
            after_lon = sum(s.lon for s in after_restart) / len(after_restart)
            moved_m = _haversine_nm(before_lat, before_lon, after_lat, after_lon) * 1852.0
            if moved_m > lock_radius_m:
                group = after_restart
    slow_enough = [s for s in group if s.sog_ms <= speed_threshold_ms / 2] or group
    settled = [s for s in slow_enough if not _engine_on_at(on_intervals, s.time)] or slow_enough
    lat = sum(s.lat for s in settled) / len(settled)
    lon = sum(s.lon for s in settled) / len(settled)
    return lat, lon


def _engine_on_at(on_intervals: List[Tuple[datetime, datetime]], t: datetime) -> bool:
    return any(start <= t <= end for start, end in on_intervals)


def _engine_on_intervals(engine_samples: List[EngineSample]) -> List[Tuple[datetime, datetime]]:
    """Merged time ranges (across all engine instances) during which an engine was actually
    running, based on fuel consumption -- a much more direct "is it running" signal than merely
    receiving PGN 127489, since some devices keep sending near-zero readings for a while after
    shutdown. Consecutive "on" readings less than ``_ENGINE_OFF_GAP_S`` apart are treated as one
    continuous interval, bridging normal reporting jitter without bridging a real shutdown."""
    on_times = sorted(
        s.time for s in engine_samples if s.fuel_rate_lph is not None and s.fuel_rate_lph > _ENGINE_IDLE_FUEL_LPH
    )
    return _merge_on_times(on_times)


def _merge_on_times(on_times: List[datetime]) -> List[Tuple[datetime, datetime]]:
    if not on_times:
        return []
    gap = timedelta(seconds=_ENGINE_OFF_GAP_S)
    intervals = [[on_times[0], on_times[0]]]
    for t in on_times[1:]:
        if t - intervals[-1][1] <= gap:
            intervals[-1][1] = t
        else:
            intervals.append([t, t])
    return [(start, end) for start, end in intervals]


def _engine_on_intervals_by_instance(
    engine_samples: List[EngineSample],
) -> Dict[int, List[Tuple[datetime, datetime]]]:
    """Same as ``_engine_on_intervals``, but kept separate per engine instance -- needed to
    extend a trip's own logged engine hours by however long that specific engine ran
    continuously right before departure and after arrival (see ``_engine_hours_delta_extended``),
    without mixing in a different engine's own on/off timing on a multi-engine boat."""
    on_times_by_instance: Dict[int, List[datetime]] = {}
    for s in engine_samples:
        if s.fuel_rate_lph is not None and s.fuel_rate_lph > _ENGINE_IDLE_FUEL_LPH:
            on_times_by_instance.setdefault(s.instance, []).append(s.time)
    return {
        instance: _merge_on_times(sorted(times)) for instance, times in on_times_by_instance.items()
    }


def _engine_off_span(
    on_intervals: List[Tuple[datetime, datetime]], run_start: datetime, run_end: datetime
) -> Optional[Tuple[datetime, datetime]]:
    """Expands a stationary run to the full time the engine was actually off around it -- from
    when it was last confirmed running just before, to when it's next confirmed running again
    just after. Without this, a long, genuinely stopped period whose *tail end* happens to look
    short and tight to noisy low-speed GPS data (found in practice: an overnight stop of 23+
    hours whose last 44 minutes, right before the engine restarted, moved less than 5 meters)
    would be mistaken for a brief lock/bridge stop, when it's really just the last fragment of a
    much longer real stay.

    Returns ``None`` if the engine isn't confirmed running again after this point, at least
    within the available data -- that can never be a lock (a lock implies the engine coming back
    on once you're through it); it's either a genuine arrival or simply where the log ends."""
    prior_ends = [end for _, end in on_intervals if end <= run_start]
    off_start = max(prior_ends) if prior_ends else run_start
    later_starts = [start for start, _ in on_intervals if start >= run_end]
    if not later_starts:
        return None
    return off_start, min(later_starts)


def _reclassify_locks(
    runs: List[Tuple[str, List[NavSample]]],
    samples: List[NavSample],
    on_intervals: List[Tuple[datetime, datetime]],
    lock_radius_m: float,
    lock_max_duration: timedelta,
) -> List[Tuple[str, List[NavSample]]]:
    """A stop is treated as a lock, opening bridge, or similarly brief operational pause -- folded
    back into the trip instead of splitting it into two -- if the engine was off no longer than
    ``lock_max_duration`` and the boat barely moved (within ``lock_radius_m`` of its own
    centroid) during the full time the engine was off (see ``_engine_off_span``, which expands
    the check beyond the stop's own, possibly SOG-noise-shortened, boundaries).

    Only applies to a stop that comes after an actual trip (i.e. not the very first run in the
    data) -- a lock is by definition something you pass through mid-voyage, never the very first
    thing recorded.

    This is a deliberately simple heuristic and knowingly conflates a lock with any other brief,
    tightly-confined pause where the engine happens to be cycled off and on again the same day
    (e.g. a quick stop at a quay) -- telling those apart would require knowing where the lock
    actually is, which is not available without unreliable external geocoding.

    A stop where the engine never confirms going off at all (_engine_off_span finds nothing to
    widen) gets the same treatment if it's still short and tightly confined by its own GPS-
    measured boundaries -- found in practice, on real data: a 10-minute stop, boat barely moving
    (well under lock_radius_m) while circling to wait for a berth to free up in a crowded marina,
    engine kept running the whole time, split what was really one continuous arrival into two
    separate logbook trips. Still gated on a later run existing (idx + 1 < len(runs)) -- the same
    "confirmed running again" reasoning _engine_off_span itself already applies for the engine-off
    case: without a next run, there's no way to tell "still waiting" from "arrived here and the
    log simply ends", so it's left as a real stop rather than guessed away."""
    relabelled = []
    for idx, (label, group) in enumerate(runs):
        if label == "stationary" and idx > 0:
            span = _engine_off_span(on_intervals, group[0].time, group[-1].time)
            if span is not None:
                off_start, off_end = span
                if off_end - off_start <= lock_max_duration:
                    span_samples = [s for s in samples if off_start <= s.time <= off_end] or group
                    if _spatial_spread_m(span_samples) <= lock_radius_m:
                        label = "moving"
            elif idx + 1 < len(runs):
                if (
                    group[-1].time - group[0].time <= lock_max_duration
                    and _spatial_spread_m(group) <= lock_radius_m
                ):
                    label = "moving"
        relabelled.append((label, group))
    return relabelled


def _split_runs_on_gaps(
    runs: List[Tuple[str, List[NavSample]]], max_gap: timedelta
) -> List[Tuple[str, List[NavSample]]]:
    """Any run can silently swallow a large data gap if the classification happens to be the same
    on both sides of it -- there's then no differently-labeled sample in between for
    ``_classify_runs`` to split on, even though we have no idea what happened during the gap.

    For a "moving" run (e.g. a moment of GPS/SOG noise right as the boat was actually stopping,
    and again once data resumes): found in practice, a trip that looked like one continuous
    ~4-hour "moving" run actually had a ~2.5-hour data gap in the middle, right after the boat had
    actually arrived; ``_moving_duration`` already excluded the gap from the reported *duration*
    correctly, but the arrival itself was never recognized as a stay, so the CSV showed the *next*
    real stay (found hours later) as the arrival port instead of the true one. Splitting at each
    internal gap >= ``max_gap`` and inserting a single-sample synthetic "stationary" stay at the
    last known position before the gap fixes that -- the same treatment as an unresolved position
    at a file/session boundary, just discovered mid-run instead of at the edges.

    For a "stationary" run: found in practice, a corrupted SD card produced ~10 hours of no/garbled
    data, with the sparse readings on both sides of it (last gasp before failure, first trickle
    after replacement) both reading near-zero speed -- so the whole 10-hour span stayed one
    unbroken "stationary" run with no synthetic marker to split it on. Downstream code (see
    ``_merge_negligible_trips``) only ever looks for gaps *between* runs, never inside one, so this
    silently merged the boat's real last-known-at-sea position with an unrelated stay found much
    later, once GPS was regained -- reporting that later port as the arrival, and drawing the
    trip's track straight to it, instead of ending at the last position actually logged before the
    failure. Simply splitting into two separate stationary runs (no synthetic point needed -- both
    sides already are "stationary") is enough; the existing between-runs gap check then keeps them
    from being merged back into one stay."""
    result: List[Tuple[str, List[NavSample]]] = []
    for label, group in runs:
        segment: List[NavSample] = [group[0]]
        for prev, curr in zip(group, group[1:]):
            if curr.time - prev.time >= max_gap:
                result.append((label, segment))
                if label == "moving":
                    result.append(("stationary", [prev]))
                segment = [curr]
            else:
                segment.append(curr)
        result.append((label, segment))
    return result


def _trip_distance_nm(group: List[NavSample]) -> float:
    return sum(_haversine_nm(a.lat, a.lon, b.lat, b.lon) for a, b in zip(group, group[1:]))


def _merge_negligible_trips(
    runs: List[Tuple[str, List[NavSample]]],
    min_leg_distance_nm: float,
    max_gap: timedelta,
    lock_radius_m: Optional[float] = None,
) -> List[Tuple[str, List[NavSample]]]:
    """A "moving" run covering less than ``min_leg_distance_nm`` doesn't get to end a trip and
    start a new stay on its own -- it's GPS/speed noise or a brief manoeuvre (e.g. nudging a few
    meters along the quay with the engine, or repositioning within the same harbour -- found in
    practice, on real data: moving under half a mile within the same harbour, engine briefly off
    in between, still split what was really one continuous arrival into two logbook trips), not a
    real trip to a new port. Deliberately a separate, looser threshold from ``min_trip_distance_nm``
    (see ``build_trips``'s own doc comment) -- that one also decides whether a *finished* trip is
    real enough to even show in the logbook at all, so raising it to cover a longer in-harbour
    manoeuvre would silently delete genuinely short, real trips between two different places
    instead of just folding an in-between leg into its surrounding stay. Its samples are real
    GPS points though, not noise to throw away: they're spliced onto the end of the nearest
    *preceding* real trip's own track, so the route drawn on the map visually reaches the boat's
    actual final position instead of stopping short at wherever it first happened to stop.
    Removing the run from ``runs`` entirely (rather than merely relabelling it) also lets the
    stationary periods on either side of it merge into a single stay for arrival-position
    purposes (see ``_merge_adjacent`` below), same effect as before.

    Also negligible, independent of ``min_leg_distance_nm``, when ``lock_radius_m`` is given and
    the run never actually strayed more than that from its own centroid (see
    ``_spatial_spread_m``) -- found in practice, on real data: several minutes of a boat sitting
    almost still at the quay (engine idling, waiting/manoeuvring to moor) with SOG noise reading
    just above ``speed_threshold_kn`` accumulated enough summed point-to-point distance (this
    function's *other* check, ``_trip_distance_nm``) to clear ``min_leg_distance_nm`` even though
    the boat was never meaningfully away from where it started -- showing up as its own bogus
    9-minute "trip" with no real destination. Summed path length is vulnerable to exactly this:
    many small jittery steps add up even when net position barely changes. Deliberately NOT the
    same as checking start-vs-end displacement instead of path length -- a real short there-and-
    back trip (motor out a couple hundred meters, turn around, return to the same berth) has
    near-zero net displacement too, and must not disappear from the logbook; what actually tells
    the two apart is whether the boat ever got meaningfully far from its own centroid at all,
    which is exactly what ``_spatial_spread_m`` (already used for lock/bridge detection, see
    ``_reclassify_locks``) measures. Reuses ``lock_radius_m`` itself rather than a separate
    parameter -- both ask the same underlying question ("did the boat stay confined to about this
    radius the whole time"), and this stays off automatically when lock/bridge detection itself is
    off (``lock_radius_m=None``), consistent with that setting's existing on/off behavior.

    Falls back to just relabelling it "stationary" -- merged into the surrounding stay, same as
    any other stationary period, contributing to its averaged position -- when there's no
    preceding trip to extend, e.g. it's the very first run in the whole dataset.

    Never splices or merges across a real data gap (``max_gap`` or more between two consecutive
    runs), no matter how short the intervening run's own distance is -- found in practice, on real
    data: a corrupted SD card produced a ~10-hour gap of no/garbled data right after a trip's
    last good position. The few scattered samples once data resumed still counted as a
    "negligible" move by distance alone, so they got spliced straight onto the *pre-gap* trip's
    own track and merged into what should have been a separate later stay -- drawing the trip's
    line straight through a harbour wall to a port the boat's logged track never actually reached,
    and reporting that port as the arrival instead of the boat's real last known position at sea.
    Distance answers "was this a real trip", not "does this belong to the same continuous visit as
    what came before it" -- only a lack of any real time gap answers that.

    (An earlier version of this fix also tracked, per merged stay, which of its samples belonged
    to the *final* sub-stay after such a splice, and averaged the arrival position over only
    those -- on real data that turned out to change nothing. Removed again as dead complexity.)"""
    result: List[Tuple[str, List[NavSample]]] = []
    last_moving_group: Optional[List[NavSample]] = None
    prev_end_time: Optional[datetime] = None
    for label, group in runs:
        if prev_end_time is not None and group[0].time - prev_end_time >= max_gap:
            last_moving_group = None  # a real data gap -- never splice/merge across it
        prev_end_time = group[-1].time

        is_negligible = _trip_distance_nm(group) < min_leg_distance_nm or (
            lock_radius_m is not None and _spatial_spread_m(group) <= lock_radius_m
        )
        if label == "moving" and is_negligible:
            if last_moving_group is not None:
                last_moving_group.extend(group)
                continue
            label = "stationary"
        if (
            label == "stationary"
            and result
            and result[-1][0] == "stationary"
            and group[0].time - result[-1][1][-1].time < max_gap
        ):
            prev_group = result[-1][1]
            prev_group.extend(group)
            continue
        result.append((label, group))
        if label == "moving":
            last_moving_group = group
    # No trailing _merge_adjacent() here (unlike the other run-relabelling passes above it) --
    # unlike those, this function's own merge branch above already handles every legitimate
    # adjacent-stationary merge itself, with the gap check that matters here; a generic unconditional
    # merge afterwards would undo exactly that check for two stationary runs that only ended up
    # adjacent because a real data gap split them apart (see _split_runs_on_gaps).
    return result


def _track_reaching_markers(
    group: List[NavSample],
    depart_time: datetime,
    depart_lat: float,
    depart_lon: float,
    arrive_time: datetime,
    arrive_lat: float,
    arrive_lon: float,
) -> List[NavSample]:
    """The drawn track (map, GPX export, periodic log table) should always visually reach its
    own departure/arrival markers -- those sit at the stay's own averaged, "settled" position
    (see TripLeg.depart_lat/arrive_lat and _settled_position()), which practically never lands
    exactly on ``group``'s own first/last raw GPS fix.

    Confirmed on real data, specifically: a Les Sables-d'Olonne arrival reached after a difficult,
    wave-tossed approach (samples still drifting/circling right up to the last one) left an 8.4m
    gap between the track's own last point and the settled arrival marker -- visibly wrong on the
    map. The very same physical stay's *departure* leg the following trip started only 0.6m from
    its own marker (the first sample after leaving is naturally close to where the boat just was),
    so the identical spot read as correct there. A nearby, unremarkable arrival (Port-Joinville,
    normal approach) had only a 1.9m gap and read as fine. The gap size tracks how much the boat
    was still moving around near the very end of a stay's own averaging window, not anything wrong
    with the marker's position itself -- but however small or large, there's no reason to leave any
    gap between a line and its own labelled endpoint when that endpoint's exact position is
    already known.

    This exact fix existed before (see this function's own git history), then was reverted without
    the underlying gap ever actually being re-examined -- it came back once the gap was traced, on
    real data, to a genuine rendering defect rather than a signal of anything else being wrong.

    A synthetic point's own speed is 0 -- it represents the boat while moored, which is what the
    average position it's placed at actually describes."""
    track = group
    if (track[0].lat, track[0].lon) != (depart_lat, depart_lon):
        start = NavSample(depart_time, depart_lat, depart_lon, 0.0, track[0].depth_m, track[0].water_temp_c)
        track = [start] + track
    if (track[-1].lat, track[-1].lon) != (arrive_lat, arrive_lon):
        end = NavSample(arrive_time, arrive_lat, arrive_lon, 0.0, track[-1].depth_m, track[-1].water_temp_c)
        track = track + [end]
    return track


def _moving_duration(group: List[NavSample], max_gap: timedelta) -> timedelta:
    """Sum of the time between consecutive points in a trip, excluding gaps >= ``max_gap``
    within it -- those don't count as "time underway", since we don't know what happened during
    such a gap. Without this, the reported duration would bridge an internal data gap (found in
    practice: a reported 3:21 duration while the engine only ran for 0:48)."""
    total = timedelta()
    for a, b in zip(group, group[1:]):
        delta = b.time - a.time
        if delta < max_gap:
            total += delta
    return total


def _engine_hours_delta(
    samples: List[EngineSample], start: datetime, end: datetime
) -> Dict[int, float]:
    by_instance: Dict[int, List[EngineSample]] = {}
    for sample in samples:
        if sample.total_hours_s is None:
            continue
        by_instance.setdefault(sample.instance, []).append(sample)

    result: Dict[int, float] = {}
    for instance, seq in by_instance.items():
        window = sorted((s for s in seq if start <= s.time <= end), key=lambda s: s.time)
        if len(window) < 2:
            continue
        delta_s = window[-1].total_hours_s - window[0].total_hours_s
        result[instance] = max(delta_s, 0) / 3600.0
    return result


def _extend_engine_window(
    depart_time: datetime,
    arrive_time: datetime,
    prev_stay: Optional[Stay],
    next_stay: Optional[Stay],
    on_intervals: List[Tuple[datetime, datetime]],
) -> Tuple[datetime, datetime]:
    """Widens [depart_time, arrive_time] to also cover however long this engine ran continuously
    right before departure and after arrival (e.g. warming up at the dock beforehand, or idling
    afterwards) -- "Gelogde motoruren" was otherwise clipped tightly to the GPS-based trip
    window, missing real engine time a boat that's essentially always under power would expect
    it to include, and reading as inconsistent with "Totale vaaruren" as a result (found in
    practice). Never reaches past the midpoint of an adjacent stay, so two trips sharing one
    continuously-running stay between them each get a fair half of it instead of double-counting
    that time in both."""
    start, end = depart_time, arrive_time
    for interval_start, interval_end in on_intervals:
        if interval_start <= depart_time <= interval_end:
            start = interval_start
            if prev_stay is not None:
                start = max(start, prev_stay.start + (prev_stay.end - prev_stay.start) / 2)
        if interval_start <= arrive_time <= interval_end:
            end = interval_end
            if next_stay is not None:
                end = min(end, next_stay.start + (next_stay.end - next_stay.start) / 2)
    return min(start, depart_time), max(end, arrive_time)


def _engine_hours_delta_extended(
    engine_samples: List[EngineSample],
    depart_time: datetime,
    arrive_time: datetime,
    prev_stay: Optional[Stay],
    next_stay: Optional[Stay],
    on_intervals_by_instance: Dict[int, List[Tuple[datetime, datetime]]],
) -> Dict[int, float]:
    """Same core computation as ``_engine_hours_delta``, but widening the window per engine
    instance first (see ``_extend_engine_window``) -- each instance gets its own window since a
    multi-engine boat's engines don't necessarily start/stop together."""
    by_instance: Dict[int, List[EngineSample]] = {}
    for sample in engine_samples:
        if sample.total_hours_s is None:
            continue
        by_instance.setdefault(sample.instance, []).append(sample)

    result: Dict[int, float] = {}
    for instance, seq in by_instance.items():
        start, end = _extend_engine_window(
            depart_time, arrive_time, prev_stay, next_stay, on_intervals_by_instance.get(instance, [])
        )
        window = sorted((s for s in seq if start <= s.time <= end), key=lambda s: s.time)
        if len(window) < 2:
            continue
        delta_s = window[-1].total_hours_s - window[0].total_hours_s
        result[instance] = max(delta_s, 0) / 3600.0
    return result


def _engine_hours_total(samples: List[EngineSample], start: datetime, end: datetime) -> Dict[int, float]:
    """Absolute engine-hour-meter reading (not a delta) at the end of the window, per engine
    instance -- the engine's own lifetime counter, e.g. for tracking maintenance intervals,
    as opposed to ``_engine_hours_delta``'s "hours run just during this trip"."""
    by_instance: Dict[int, List[EngineSample]] = {}
    for sample in samples:
        if sample.total_hours_s is None:
            continue
        by_instance.setdefault(sample.instance, []).append(sample)

    result: Dict[int, float] = {}
    for instance, seq in by_instance.items():
        window = sorted((s for s in seq if start <= s.time <= end), key=lambda s: s.time)
        if not window:
            continue
        result[instance] = window[-1].total_hours_s / 3600.0
    return result


def _fuel_liters(samples: List[EngineSample], start: datetime, end: datetime) -> float:
    by_instance: Dict[int, List[EngineSample]] = {}
    for sample in samples:
        if sample.fuel_rate_lph is None:
            continue
        by_instance.setdefault(sample.instance, []).append(sample)

    total = 0.0
    for seq in by_instance.values():
        window = sorted((s for s in seq if start <= s.time <= end), key=lambda s: s.time)
        for a, b in zip(window, window[1:]):
            dt_h = (b.time - a.time).total_seconds() / 3600.0
            if dt_h <= 0 or dt_h > _MAX_INTEGRATION_GAP_H:
                continue
            total += dt_h * (a.fuel_rate_lph + b.fuel_rate_lph) / 2
    return total


def _device_fuel_delta(
    samples: List[TripFuelSample], start: datetime, end: datetime
) -> Optional[float]:
    """Difference between the start and end reading of the engine's own trip meter within the
    time window.

    Returns None if this PGN wasn't (sufficiently) available for this trip -- e.g. because the
    device doesn't send it -- instead of a misleading 0.
    """
    by_instance: Dict[int, List[TripFuelSample]] = {}
    for sample in samples:
        if sample.trip_fuel_used_l is None:
            continue
        by_instance.setdefault(sample.instance, []).append(sample)

    total = 0.0
    found_any = False
    for seq in by_instance.values():
        window = sorted((s for s in seq if start <= s.time <= end), key=lambda s: s.time)
        if len(window) < 2:
            continue
        delta = window[-1].trip_fuel_used_l - window[0].trip_fuel_used_l
        total += max(delta, 0.0)
        found_any = True
    return total if found_any else None


def _speed_stats_kn(track: List[NavSample]) -> Tuple[Optional[float], Optional[float], Optional[datetime]]:
    """Returns (avg, max, time of the max) -- the time lets a caller look up what else was going
    on (e.g. engine RPM, see _rpm_at_time) at the exact moment of the trip's top speed."""
    if not track:
        return None, None, None
    speeds_kn = [s.sog_ms / _KNOT_IN_MS for s in track]
    max_idx = max(range(len(track)), key=lambda i: speeds_kn[i])
    return sum(speeds_kn) / len(speeds_kn), speeds_kn[max_idx], track[max_idx].time


def _min_depth(track: List[NavSample]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Returns (depth_m, lat, lon) of the shallowest measured point, or (None, None, None)."""
    candidates = [s for s in track if s.depth_m is not None]
    if not candidates:
        return None, None, None
    shallowest = min(candidates, key=lambda s: s.depth_m)
    return shallowest.depth_m, shallowest.lat, shallowest.lon


def _water_temp_stats(track: List[NavSample]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Returns (avg, min, max) water temperature in degrees Celsius, or (None, None, None)."""
    values = [s.water_temp_c for s in track if s.water_temp_c is not None]
    if not values:
        return None, None, None
    return sum(values) / len(values), min(values), max(values)


def _engine_health(
    samples: List[EngineSample], start: datetime, end: datetime
) -> Dict[int, EngineHealth]:
    by_instance: Dict[int, List[EngineSample]] = {}
    for sample in samples:
        by_instance.setdefault(sample.instance, []).append(sample)

    def _avg(values: List[float]) -> Optional[float]:
        return sum(values) / len(values) if values else None

    result: Dict[int, EngineHealth] = {}
    for instance, seq in by_instance.items():
        window = [s for s in seq if start <= s.time <= end]
        if not window:
            continue
        oil_pressure = [s.oil_pressure_pa for s in window if s.oil_pressure_pa is not None]
        oil_temperature = [s.oil_temperature_k for s in window if s.oil_temperature_k is not None]
        coolant_temperature = [s.coolant_temperature_k for s in window if s.coolant_temperature_k is not None]
        alternator_voltage = [s.alternator_voltage_v for s in window if s.alternator_voltage_v is not None]
        engine_load = [s.engine_load_pct for s in window if s.engine_load_pct is not None]
        warnings: FrozenSet[str] = frozenset().union(*(s.warnings for s in window))
        warning_first_seen: Dict[str, datetime] = {}
        for sample in sorted(window, key=lambda s: s.time):
            for warning in sample.warnings:
                warning_first_seen.setdefault(warning, sample.time)

        oil_pressure_avg = _avg(oil_pressure)
        oil_temperature_avg = _avg(oil_temperature)
        coolant_temperature_avg = _avg(coolant_temperature)

        result[instance] = EngineHealth(
            oil_pressure_bar_avg=oil_pressure_avg * _PA_TO_BAR if oil_pressure_avg is not None else None,
            oil_temperature_c_avg=oil_temperature_avg - _KELVIN_TO_CELSIUS if oil_temperature_avg is not None else None,
            coolant_temperature_c_avg=coolant_temperature_avg - _KELVIN_TO_CELSIUS
            if coolant_temperature_avg is not None
            else None,
            alternator_voltage_v_avg=_avg(alternator_voltage),
            engine_load_pct_max=max(engine_load) if engine_load else None,
            warnings=warnings,
            warning_first_seen=warning_first_seen,
        )
    return result


def _battery_health(samples: List[BatterySample], start: datetime, end: datetime) -> Dict[int, BatteryHealth]:
    by_instance: Dict[int, List[BatterySample]] = {}
    for sample in samples:
        if sample.voltage_v is None:
            continue
        by_instance.setdefault(sample.instance, []).append(sample)

    result: Dict[int, BatteryHealth] = {}
    for instance, seq in by_instance.items():
        window = [s for s in seq if start <= s.time <= end]
        if not window:
            continue
        voltages = [s.voltage_v for s in window]
        min_sample = min(window, key=lambda s: s.voltage_v)
        result[instance] = BatteryHealth(
            avg_voltage_v=sum(voltages) / len(voltages),
            min_voltage_v=min_sample.voltage_v,
            min_voltage_at=min_sample.time,
        )
    return result


def _rpm_at_time(
    rpm_samples: List[EngineRpmSample], time: datetime, start: datetime, end: datetime
) -> Dict[int, float]:
    """The RPM reading closest to ``time`` (e.g. the moment of the trip's max speed), per engine
    instance -- restricted to this trip's own [start, end] window so a gap in RPM reporting right
    at that moment doesn't pick up a reading that actually belongs to a different trip."""
    by_instance: Dict[int, List[EngineRpmSample]] = {}
    for sample in rpm_samples:
        if sample.rpm is None or not (start <= sample.time <= end):
            continue
        by_instance.setdefault(sample.instance, []).append(sample)
    return {
        instance: min(samples, key=lambda s: abs((s.time - time).total_seconds())).rpm
        for instance, samples in by_instance.items()
    }


def _typical_rpm(samples: List[EngineRpmSample], start: datetime, end: datetime) -> Dict[int, float]:
    """The most commonly occurring engine speed (RPM) during the trip, per engine instance --
    rounded to the nearest ``_RPM_BUCKET`` before counting, so normal small load fluctuations at
    a steady cruising speed don't get spread across too many distinct exact values to ever "win".
    This is a more representative "cruising RPM" than an average (skewed by idle/neutral periods
    and maneuvering) or a maximum (skewed by brief revs)."""
    by_instance: Dict[int, List[float]] = {}
    for sample in samples:
        if sample.rpm is None or not (start <= sample.time <= end):
            continue
        by_instance.setdefault(sample.instance, []).append(sample.rpm)

    result: Dict[int, float] = {}
    for instance, values in by_instance.items():
        if not values:
            continue
        buckets = Counter(round(v / _RPM_BUCKET) * _RPM_BUCKET for v in values)
        result[instance] = float(buckets.most_common(1)[0][0])
    return result


def _typical_rpm_speed_range(
    rpm_samples: List[EngineRpmSample],
    engine_samples: List[EngineSample],
    track: List[NavSample],
    start: datetime,
    end: datetime,
) -> Dict[int, Tuple[float, float, float, Optional[float]]]:
    """(min, max, avg) boat speed in knots, plus average fuel consumption (L/nm, None if no fuel
    data), recorded at the moments the engine was actually running at its typical RPM (see
    ``_typical_rpm``) -- the trip's overall average speed is diluted by slower maneuvering in/out
    of the harbor, so "2250 RPM" next to "11.9 kn avg" reads as if that RPM only makes 11.9 kn,
    when the boat was really doing 12.6-13.4 kn whenever it was actually holding that RPM (found
    in practice).

    Only counts samples from a *sustained* run at (or within one bucket of) the typical RPM
    bucket -- at least ``_RPM_STABLE_MINUTES`` long -- a lone reading that briefly passes through
    that exact RPM while accelerating or decelerating isn't steady cruising, and including it
    widened the range far beyond what the boat was actually doing at a held RPM (found in
    practice: a 2250 RPM trip showing an 8-17.6 kn range instead of the ~2 kn spread a steady
    cruise actually has). The one-bucket tolerance matters in practice too: an engine genuinely
    holding a steady RPM still hunts back and forth by a bucket or two from second to second
    (throttle/governor jitter, sea state), so requiring the *exact* same bucket for the whole
    window fragmented an obviously-steady 40+ minute cruise into dozens of sub-minute runs, none
    of which ever reached the duration threshold on its own."""
    if not track:
        return {}
    times = [s.time for s in track]

    by_instance: Dict[int, List[EngineRpmSample]] = {}
    for sample in rpm_samples:
        if sample.rpm is None or not (start <= sample.time <= end):
            continue
        by_instance.setdefault(sample.instance, []).append(sample)

    fuel_by_instance: Dict[int, List[EngineSample]] = {}
    for sample in engine_samples:
        if sample.fuel_rate_lph is None or not (start <= sample.time <= end):
            continue
        fuel_by_instance.setdefault(sample.instance, []).append(sample)

    min_duration = timedelta(minutes=_RPM_STABLE_MINUTES)
    result: Dict[int, Tuple[float, float, float, Optional[float]]] = {}
    for instance, samples in by_instance.items():
        samples = sorted(samples, key=lambda s: s.time)
        buckets = [round(s.rpm / _RPM_BUCKET) * _RPM_BUCKET for s in samples]
        counts = Counter(buckets)
        if not counts:
            continue
        typical_bucket = counts.most_common(1)[0][0]
        near_typical = [abs(b - typical_bucket) <= _RPM_BUCKET for b in buckets]

        speeds_kn = []
        windows: List[Tuple[datetime, datetime]] = []
        idx = 0
        n = len(samples)
        while idx < n:
            if not near_typical[idx]:
                idx += 1
                continue
            j = idx
            while j + 1 < n and near_typical[j + 1]:
                j += 1
            if samples[j].time - samples[idx].time >= min_duration:
                windows.append((samples[idx].time, samples[j].time))
                for sample in samples[idx : j + 1]:
                    pos = bisect.bisect_left(times, sample.time)
                    candidates = [i for i in (pos - 1, pos) if 0 <= i < len(track)]
                    if not candidates:
                        continue
                    nearest = min(candidates, key=lambda i: abs((track[i].time - sample.time).total_seconds()))
                    speeds_kn.append(track[nearest].sog_ms / _KNOT_IN_MS)
            idx = j + 1

        if not speeds_kn:
            continue

        avg_kn = sum(speeds_kn) / len(speeds_kn)

        # Same sustained windows as the speed samples above, not the whole trip -- fuel burn
        # while idling/maneuvering at a different RPM shouldn't dilute "what does it cost to hold
        # this RPM" any more than the trip's overall average speed should.
        fuel_rates = [
            fuel_sample.fuel_rate_lph
            for fuel_sample in fuel_by_instance.get(instance, [])
            if any(w_start <= fuel_sample.time <= w_end for w_start, w_end in windows)
        ]
        # L/nm (matching the rest of the logbook, e.g. the per-trip "Gem. verbruik") rather than
        # L/h -- L/h alone doesn't say anything about efficiency, since a higher RPM naturally
        # burns more per hour but may still cover a mile more efficiently.
        avg_fuel_l_per_nm = (sum(fuel_rates) / len(fuel_rates) / avg_kn) if fuel_rates and avg_kn > 0 else None
        result[instance] = (min(speeds_kn), max(speeds_kn), avg_kn, avg_fuel_l_per_nm)
    return result


def _motion_variation(
    sorted_samples: List[AttitudeSample], start: datetime, end: datetime
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Returns (roll_stdev, pitch_stdev, roll_range, pitch_range) from roll/pitch (PGN 127257)
    during the trip -- a rougher sea or more wave action shows up as more variation in how the
    boat's attitude moves around, even on a boat holding a level average heel/trim. The standard
    deviation reflects the *typical* motion across the whole trip, but a trip that's mostly calm
    with one rough patch will still average out to a small number there; the peak-to-peak range
    (max - min) instead captures the single worst swing, which is closer to what you'd remember
    feeling (found in practice: a trip with stdev 2.5 deg still had a roll range of ~23 deg).
    Neither is an established metric (unlike e.g. significant wave height, which needs a wave
    sensor this app doesn't have); just relative indicators from whatever motion sensor is
    already on the network.

    ``sorted_samples`` must already be sorted by time (see build_trips, which sorts once up
    front) -- this is called once per trip, and a real log can have millions of attitude samples
    spanning many days, so re-scanning the *entire* list per trip to filter down to its own
    window is real, measured cost (found in practice: ~21s of a ~37s run, for just 18 calls)
    that a one-off sort + bisect avoids almost entirely."""
    lo = bisect.bisect_left(sorted_samples, start, key=lambda s: s.time)
    hi = bisect.bisect_right(sorted_samples, end, key=lambda s: s.time)
    window = sorted_samples[lo:hi]
    rolls = [s.roll_deg for s in window if s.roll_deg is not None]
    pitches = [s.pitch_deg for s in window if s.pitch_deg is not None]
    roll_stdev = statistics.stdev(rolls) if len(rolls) >= 2 else None
    pitch_stdev = statistics.stdev(pitches) if len(pitches) >= 2 else None
    roll_range = max(rolls) - min(rolls) if rolls else None
    pitch_range = max(pitches) - min(pitches) if pitches else None
    return roll_stdev, pitch_stdev, roll_range, pitch_range


def build_trips(
    fixes: FixArray | List[PositionFix],
    sogs: SogArray | List[SogSample],
    engine_samples: List[EngineSample],
    trip_fuel_samples: Optional[List[TripFuelSample]] = None,
    depth_samples: Optional[List[DepthSample]] = None,
    water_temp_samples: Optional[List[WaterTempSample]] = None,
    battery_samples: Optional[List[BatterySample]] = None,
    rpm_samples: Optional[List[EngineRpmSample]] = None,
    attitude_samples: Optional[List[AttitudeSample]] = None,
    *,
    geocoder: Optional[object] = None,
    speed_threshold_kn: float = 0.5,
    min_stop_minutes: float = 10.0,
    max_gap_minutes: Optional[float] = None,
    min_trip_distance_nm: float = 0.1,
    min_leg_distance_nm: Optional[float] = None,
    lock_radius_m: Optional[float] = None,
    lock_max_duration_minutes: Optional[float] = None,
) -> List[TripLeg]:
    """``max_gap_minutes``: how long there can be no data at most before a trip's reported
    duration gets cut off (see ``_moving_duration``). Defaults to the same value as
    ``min_stop_minutes`` -- the same number, but two different meanings: one is "how long do
    you have to be stationary", the other "how long can there be no data". Ports are still
    linked across such a gap (see ``_merge_short_stops``) -- only the trip's *duration* ignores
    the gap, not the departure/arrival port itself.

    ``min_leg_distance_nm``: how short an in-transit "moving" leg between two stays can be before
    it's folded into its surrounding stay instead of ending one trip and starting another (see
    ``_merge_negligible_trips``) -- e.g. repositioning within the same harbour. Deliberately a
    separate, looser threshold from ``min_trip_distance_nm`` below: that one also decides whether
    an already-finished trip is real enough to appear in the logbook at all, so it has to stay
    tight -- raising it to cover a longer in-harbour manoeuvre would silently delete genuinely
    short, real trips between two different places instead of just merging an in-between leg.
    Defaults to the same value as ``min_trip_distance_nm`` when left unset, keeping today's
    behavior for any caller that doesn't know about this distinction yet.

    ``min_trip_distance_nm``: trips covering less than this are filtered out. This is
    GPS/speed noise (a few seconds just above ``speed_threshold_kn``), not a real trip (found
    in practice: 0.0 nm, lasting a few seconds to minutes, engine off).

    ``lock_radius_m`` / ``lock_max_duration_minutes``: a stop is treated as a lock/bridge rather
    than a port visit if the engine was off no longer than ``lock_max_duration_minutes`` and the
    boat stayed within ``lock_radius_m`` of its own position the whole time (see
    ``_reclassify_locks``). Both default to ``None`` (disabled) at this level -- the CLI turns
    this on with sensible defaults; left off here so callers/tests that don't care about it get
    the plain speed-based behavior."""
    # Callers that already accumulate season-wide data as FixArray/SogArray (see fix_array.py --
    # cli.py/android_entry.py do, to avoid ever holding millions of PositionFix/SogSample objects
    # at once) pass those straight through; anything else (a plain list, as every existing test
    # in this file still constructs) is wrapped here so this function's own public contract
    # doesn't change for any existing caller.
    if not isinstance(fixes, FixArray):
        fixes = FixArray(fixes)
    if not isinstance(sogs, SogArray):
        sogs = SogArray(sogs)

    if geocoder is None:
        geocoder = NoGeocoder()
    if trip_fuel_samples is None:
        trip_fuel_samples = []
    if battery_samples is None:
        battery_samples = []
    if rpm_samples is None:
        rpm_samples = []
    if attitude_samples is None:
        attitude_samples = []
    # AttitudeArray gets its own array-native sort (see fix_array.py) -- a plain sorted(...)
    # would iterate it into a fully-materialized list of AttitudeSample objects that then lives
    # for the rest of this function's run (passed to _motion_variation for every trip), silently
    # undoing the point of storing a season's worth of them as array.array columns in the first
    # place.
    attitude_samples = (
        attitude_samples.sorted_by_time()
        if isinstance(attitude_samples, AttitudeArray)
        else sorted(attitude_samples, key=lambda s: s.time)
    )
    if max_gap_minutes is None:
        max_gap_minutes = min_stop_minutes

    # Checkpoints through here (not per-trip below -- a real season is typically a few dozen
    # trips at most, fast either way) -- found in practice: this whole function used to run
    # completely silent, on a phone's much slower CPU, over however many merged NavSample points
    # a full multi-year archive produces, with nothing to tell "still working" apart from "hung"
    # or "crashed silently" for however long it took.
    samples = _merge_nav_samples(fixes, sogs, depth_samples, water_temp_samples)
    if len(samples) < 2:
        return []
    log(f"[info] ...{len(samples)} navigation samples merged, classifying trips...")

    speed_threshold_ms = speed_threshold_kn * _KNOT_IN_MS
    min_stop = timedelta(minutes=min_stop_minutes)
    max_gap = timedelta(minutes=max_gap_minutes)

    # Needed regardless of the lock/bridge settings below -- also used to widen each trip's own
    # "Gelogde motoruren" with however long its engine ran continuously right before departure
    # and after arrival (see _extend_engine_window), and by _settled_position below (every stay,
    # not just when lock/bridge detection is on) to keep excluding a still-under-power docking
    # manoeuvre from a stay's own averaged position even past the point its speed alone already
    # reads as "stopped" -- asked for explicitly: while the engine's still running, the skipper
    # may still be actively working the boat into its final spot (bow thruster nudges, reversing,
    # ...), not yet genuinely at rest, regardless of what the instantaneous SOG says.
    on_intervals_by_instance = _engine_on_intervals_by_instance(engine_samples)
    on_intervals = _engine_on_intervals(engine_samples)

    runs = _classify_runs(samples, speed_threshold_ms)
    runs = _merge_short_stops(runs, min_stop, max_gap)
    if lock_radius_m is not None and lock_max_duration_minutes is not None:
        lock_max_duration = timedelta(minutes=lock_max_duration_minutes)
        runs = _reclassify_locks(runs, samples, on_intervals, lock_radius_m, lock_max_duration)
        runs = _merge_adjacent(runs)
    runs = _split_runs_on_gaps(runs, max_gap)
    effective_min_leg_distance_nm = (
        min_leg_distance_nm if min_leg_distance_nm is not None else min_trip_distance_nm
    )
    runs = _merge_negligible_trips(runs, effective_min_leg_distance_nm, max_gap, lock_radius_m)
    log(f"[info] ...{len(runs)} run(s) classified, computing per-trip statistics...")

    stays: List[Optional[Stay]] = []
    for label, group in runs:
        if label != "stationary":
            stays.append(None)
            continue
        lat, lon = _settled_position(group, speed_threshold_ms, on_intervals, lock_radius_m)
        place = geocoder.place_name(lat, lon)
        stays.append(Stay(group[0].time, group[-1].time, lat, lon, place))

    trips: List[TripLeg] = []
    for idx, (label, group) in enumerate(runs):
        if label != "moving":
            continue
        prev_stay = stays[idx - 1] if idx > 0 else None
        next_stay = stays[idx + 1] if idx + 1 < len(stays) else None

        depart_time = prev_stay.end if prev_stay else group[0].time
        arrive_time = next_stay.start if next_stay else group[-1].time
        depart_place = prev_stay.place if prev_stay else "Unknown (start outside log file)"
        arrive_place = next_stay.place if next_stay else "Unknown (end outside log file)"
        depart_lat = prev_stay.lat if prev_stay else group[0].lat
        depart_lon = prev_stay.lon if prev_stay else group[0].lon
        arrive_lat = next_stay.lat if next_stay else group[-1].lat
        arrive_lon = next_stay.lon if next_stay else group[-1].lon

        distance_nm = sum(
            _haversine_nm(a.lat, a.lon, b.lat, b.lon) for a, b in zip(group, group[1:])
        )
        avg_speed_kn, max_speed_kn, max_speed_at = _speed_stats_kn(group)
        max_speed_rpm = (
            _rpm_at_time(rpm_samples, max_speed_at, depart_time, arrive_time) if max_speed_at else {}
        )
        min_depth_m, min_depth_lat, min_depth_lon = _min_depth(group)
        avg_water_temp_c, min_water_temp_c, max_water_temp_c = _water_temp_stats(group)
        roll_variation_deg, pitch_variation_deg, roll_range_deg, pitch_range_deg = _motion_variation(
            attitude_samples, depart_time, arrive_time
        )

        trips.append(
            TripLeg(
                depart_time=depart_time,
                arrive_time=arrive_time,
                depart_place=depart_place,
                arrive_place=arrive_place,
                depart_lat=depart_lat,
                depart_lon=depart_lon,
                arrive_lat=arrive_lat,
                arrive_lon=arrive_lon,
                duration=_moving_duration(group, max_gap),
                distance_nm=distance_nm,
                avg_speed_kn=avg_speed_kn,
                max_speed_kn=max_speed_kn,
                fuel_liters=_fuel_liters(engine_samples, depart_time, arrive_time),
                fuel_liters_device=_device_fuel_delta(trip_fuel_samples, depart_time, arrive_time),
                engine_hours=_engine_hours_delta_extended(
                    engine_samples, depart_time, arrive_time, prev_stay, next_stay, on_intervals_by_instance
                ),
                engine_hours_total=_engine_hours_total(engine_samples, depart_time, arrive_time),
                engine_health=_engine_health(engine_samples, depart_time, arrive_time),
                typical_rpm=_typical_rpm(rpm_samples, depart_time, arrive_time),
                typical_rpm_speed_kn=_typical_rpm_speed_range(
                    rpm_samples, engine_samples, group, depart_time, arrive_time
                ),
                battery_health=_battery_health(battery_samples, depart_time, arrive_time),
                min_depth_m=min_depth_m,
                min_depth_lat=min_depth_lat,
                min_depth_lon=min_depth_lon,
                avg_water_temp_c=avg_water_temp_c,
                min_water_temp_c=min_water_temp_c,
                max_water_temp_c=max_water_temp_c,
                roll_variation_deg=roll_variation_deg,
                pitch_variation_deg=pitch_variation_deg,
                roll_range_deg=roll_range_deg,
                pitch_range_deg=pitch_range_deg,
                track=_track_reaching_markers(
                    group, depart_time, depart_lat, depart_lon, arrive_time, arrive_lat, arrive_lon
                ),
                max_speed_at=max_speed_at,
                max_speed_rpm=max_speed_rpm,
            )
        )
    return [trip for trip in trips if trip.distance_nm >= min_trip_distance_nm]
