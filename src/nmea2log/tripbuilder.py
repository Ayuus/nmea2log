"""Turns sequences of positions/speeds/engine data into logbook trips (departure port ->
arrival port).

Approach: every GPS fix is classified as 'stationary' or 'underway' based on speed over
ground. Consecutive stationary periods that last long enough (threshold ``min_stop_minutes``)
are considered a port visit; the periods in between are the trips. For each trip, fuel
consumption is calculated by integrating the fuel-rate readings (PGN 127489, engine data) over
time -- so explicitly not via a tank sensor.
"""

from __future__ import annotations

import array
import bisect
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Dict, FrozenSet, Iterable, Iterator, List, Optional, Tuple, Union

from .fix_array import (
    _CLOCK_JUMP_THRESHOLD_S,
    AttitudeArray,
    BatteryArray,
    DepthArray,
    EngineArray,
    FixArray,
    NavSampleArray,
    RpmArray,
    SogArray,
    TripFuelArray,
    WaterTempArray,
    _from_epoch,
    _log_time_anomaly,
    _to_epoch,
)
from .geocode import NoGeocoder
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
TRIP_LOGIC_VERSION = 7
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


# A "run" (a maximal stretch classified 'stationary' or 'moving', see _classify_runs) used to be
# represented as a List[NavSample] -- one boxed Python object per GPS fix, kept alive for
# build_trips()'s entire run. On a real multi-year archive (millions of fixes) that is the single
# biggest memory cost left in the whole pipeline (see NavSampleArray's own docstring in
# fix_array.py) -- confirmed in practice: this is what actually got the Android app OOM-killed by
# the OS on a full, from-scratch archive rebuild.
#
# A Group instead holds (start, end) half-open index ranges into a single, season-wide
# NavSampleArray -- normally just one range (a run is a contiguous slice of the array), except
# after _merge_negligible_trips splices a later, non-adjacent run's samples onto an earlier trip's
# own track (see that function's own docstring), where a Group ends up holding more than one
# range, in time order. Because Group is a plain list of lightweight (int, int) tuples, every
# existing list operation the old List[NavSample]-based code relied on (concatenation, .extend(),
# slicing, indexing group[0]/group[-1]) keeps working completely unchanged -- only code that used
# to read an element's own .time/.lat/... attributes needs a samples.xxx_at(i) lookup instead.
#
# Only right before a "leaf" per-trip statistics function that needs real attribute access (e.g.
# _settled_position, _speed_stats_kn, _track_reaching_markers) does a Group actually get turned
# back into a real List[NavSample], via _materialize() below -- always a single trip's or stay's
# own, already-small selection of samples, never the whole season, so that stays cheap regardless
# of how large the underlying archive is. Those leaf functions themselves are entirely unchanged.
Group = List[Tuple[int, int]]


def _group_start_time(samples: NavSampleArray, group: Group) -> datetime:
    return samples.datetime_at(group[0][0])


def _group_end_time(samples: NavSampleArray, group: Group) -> datetime:
    return samples.datetime_at(group[-1][1] - 1)


def _group_index_pairs(group: Group) -> Iterator[Tuple[int, int]]:
    """Yields consecutive (i, j) index pairs across a group's ranges, in the same order
    zip(flat, flat[1:]) over the group's fully-materialized samples would produce -- including the
    boundary pair between two ranges spliced together by _merge_negligible_trips -- without ever
    materializing that flattened sequence itself."""
    prev: Optional[int] = None
    for start, end in group:
        if prev is not None:
            yield prev, start
        for i in range(start, end - 1):
            yield i, i + 1
        prev = end - 1


def _materialize(samples: NavSampleArray, group: Group) -> List[NavSample]:
    """The one place a group's samples get turned back into real NavSample objects -- see Group's
    own docstring above."""
    result: List[NavSample] = []
    for start, end in group:
        for i in range(start, end):
            result.append(
                NavSample(
                    samples.datetime_at(i),
                    samples.lat_at(i),
                    samples.lon_at(i),
                    samples.sog_at(i),
                    samples.depth_at(i),
                    samples.water_temp_at(i),
                    samples.cog_at(i),
                )
            )
    return result


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

# A fix must also be consistent with the receiver's *own* speed over ground (PGN 129026): position
# and speed come from the same device, so a boat reporting 0.3 kn cannot have moved 50 m in a
# second. Confirmed on a real boat: for ~100 s after every GPS power-on the reported position kept
# gliding towards the real one (53, 26, 16, 12, 8 m per second, ...) while SOG stayed at 0.1-0.3 kn
# -- and a valid HDOP was already being reported for most of that time (see gnss_gate.py), so the
# receiver's own fix-quality report alone doesn't catch it. A fix is rejected when it is further
# from the last accepted one than _SOG_CHECK_SLACK_M plus what _SOG_CHECK_FACTOR times the reported
# SOG (plus _SOG_CHECK_SLACK_MS, ~3 kn) could cover in the elapsed time. Only checked across short
# gaps: over a longer one the boat may well have moved and stopped again since.
_SOG_CHECK_SLACK_M = 10.0
_SOG_CHECK_FACTOR = 3.0
_SOG_CHECK_SLACK_MS = 1.5
_SOG_CHECK_MAX_DT_S = 30.0

_CLOCK_JUMP_THRESHOLD_HOURS = _CLOCK_JUMP_THRESHOLD_S / 3600.0  # see fix_array.py

# The outlier log lines say *when* the dropped fixes were, so a start-up glitch can be told from a
# jump in the middle of a trip: fixes at most this far apart are one stretch, and only the first
# few stretches are spelled out (a broken source could otherwise produce a very long line).
_STRETCH_GAP_S = 30.0
_MAX_STRETCHES_LOGGED = 8

# A receiver keeps settling on its position for a while after power-on even though it already
# reports a good fix (found in practice: fix mode 3D and HDOP 1.1-1.8 while the reported position
# still glided ~16 m per second for 30 s, so a fix-quality rule can't catch it -- only the
# plausibility checks below do). Fixes dropped within _POWER_ON_SETTLE_S of a power-on -- the first
# fix after at least _POWER_ON_GAP_S without any accepted one, or the very first fix -- are that
# known behaviour and logged as plain info; anything dropped elsewhere is a real [anomaly].
_POWER_ON_GAP_S = 300.0
_POWER_ON_SETTLE_S = 120.0


@dataclass
class _Stretch:
    first: float
    last: float
    count: int


class _DroppedTimes:
    """The times (epoch seconds) of the fixes one outlier check dropped, kept as stretches."""

    def __init__(self) -> None:
        self.stretches: List[_Stretch] = []
        self.total = 0

    def add(self, epoch: float) -> None:
        self.total += 1
        if self.stretches and epoch - self.stretches[-1].last <= _STRETCH_GAP_S:
            self.stretches[-1].last = epoch
            self.stretches[-1].count += 1
        else:
            self.stretches.append(_Stretch(epoch, epoch, 1))

    def describe(self) -> str:
        """``2026-07-30 12:39:12 until 2026-07-30 12:39:41 UTC (258 fixes); ...``"""
        parts = []
        for stretch in self.stretches[:_MAX_STRETCHES_LOGGED]:
            text = f"{_from_epoch(stretch.first):%Y-%m-%d %H:%M:%S}"
            if stretch.last != stretch.first:
                text += f" until {_from_epoch(stretch.last):%Y-%m-%d %H:%M:%S}"
            parts.append(f"{text} UTC ({stretch.count} {'fix' if stretch.count == 1 else 'fixes'})")
        more = len(self.stretches) - _MAX_STRETCHES_LOGGED
        if more > 0:
            parts.append(f"and {more} more")
        return "; ".join(parts)


def _reject_gps_outliers_array(fixes: FixArray, sogs: Optional[SogArray] = None) -> FixArray:
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

    Comparing against the last *accepted* fix, not simply the previous one, is what lets a single
    bad fix get dropped without also rejecting the good fix right after it. Operates directly on
    the FixArray's columns, so a run never has to materialize a full PositionFix object per fix
    just to filter them.

    ``sogs`` (already in time order), when given, adds a second check: a fix must also be
    consistent with the receiver's own speed over ground -- see _SOG_CHECK_SLACK_M above.

    Mutates and returns the same ``fixes`` object it was given (see FixArray.replace_columns_with)
    rather than building and returning an unrelated new one -- a caller that keeps its own
    reference to this exact object across build_trips()'s whole run (every real caller does, see
    pipeline.py) sees the outlier-rejected, sorted data too, and the original, larger
    columns are freed immediately instead of staying resident for no reason.

    Processes ``fixes`` in its own natural (file-discovery) order, never sorting it first --
    sorted(range(n), key=...) has to materialize a full list of boxed index *and* key objects for
    the whole array just to sort it at all, confirmed (via fine-grained checkpoint logging on a
    real device) to be exactly where a full-archive rebuild was getting OOM-killed. That sort
    turned out to be solving the wrong problem anyway: confirmed, on the same real archive, that
    the handful of backward jumps in a real season's data (up to ~465 out of 2.7 million fixes)
    aren't file-ordering noise at all -- they're a real device logging with a stale/uncorrected
    clock for a few readings before its first accurate PGN 126992 (System Time) update (every
    frame's own timestamp is simply whichever System Time reading was last seen -- see
    ebl_reader.py's own docstring), off by whole weeks in the cases actually seen. Reordering a
    wrong timestamp into a different position wouldn't have made it a right one; the dt_hours <= 0
    check below already drops exactly this kind of reading (a fix that doesn't move time forward
    relative to the last *accepted* one), sorted or not.

    A backward jump of at least _CLOCK_JUMP_THRESHOLD_HOURS is treated as a clock-sync
    correction, not a bad reading -- confirmed in practice: a first version of this that always
    rejected any dt_hours <= 0 fix got permanently anchored on the *wrong*, weeks-in-the-future
    fix once one of these jumps happened, then kept rejecting every genuinely good, correctly-
    timed fix that followed (they all looked "earlier" than that wrong anchor) until real time
    caught all the way back up to it -- on a real ~2-month archive with a ~37-day clock-sync
    jump near its start, that silently threw away nearly the entire season (2.7 million fixes
    in, 27 left). Past that threshold, the *earlier* reading is far more likely to be the
    device's own pre-sync clock than the boat teleporting into the future, so this trusts the
    new, lower reading and resets from there instead."""
    n = len(fixes)
    if n == 0:
        return fixes
    accepted = FixArray()
    prev_i = 0
    accepted.append_raw(fixes.time_at(0), fixes.lat_at(0), fixes.lon_at(0))
    backward_dropped = clock_resets = 0
    implausible_times, sog_inconsistent_times = _DroppedTimes(), _DroppedTimes()
    implausible_startup, sog_inconsistent_startup = _DroppedTimes(), _DroppedTimes()
    session_start = fixes.time_at(0)  # the last power-on: see _POWER_ON_GAP_S
    max_backward_s = 0.0
    first_backward_at: Optional[float] = None
    sog_idx = 0
    current_sog_ms: Optional[float] = None
    n_sogs = len(sogs) if sogs is not None else 0
    for i in range(1, n):
        fix_time = fixes.time_at(i)
        dt_hours = (fix_time - fixes.time_at(prev_i)) / 3600
        if dt_hours <= -_CLOCK_JUMP_THRESHOLD_HOURS:
            # A large backward jump -- prev_i's own reading (and whatever anomaly led up to it)
            # is almost certainly the wrong one, not this one. Trust this reading and reset the
            # anchor here, instead of rejecting it and staying stuck comparing everything after
            # it against a wrong, far-future anchor.
            clock_resets += 1
            accepted.append_raw(fix_time, fixes.lat_at(i), fixes.lon_at(i))
            prev_i = i
            sog_idx, current_sog_ms = 0, None  # the speed samples' clock reset along with it
            continue
        if dt_hours <= 0:
            # Equal timestamps are routine (every frame between two PGN 126992 updates shares the
            # same time) and quietly dropped; only a genuine step backward is worth reporting.
            if dt_hours < 0:
                backward_dropped += 1
                max_backward_s = max(max_backward_s, -dt_hours * 3600)
                if first_backward_at is None:
                    first_backward_at = fix_time
            continue  # duplicate/out-of-order timestamp -- keep whichever came first
        while sog_idx < n_sogs and sogs.time_at(sog_idx) <= fix_time:
            current_sog_ms = sogs.sog_at(sog_idx)
            sog_idx += 1
        distance_nm = _haversine_nm(fixes.lat_at(prev_i), fixes.lon_at(prev_i), fixes.lat_at(i), fixes.lon_at(i))
        if distance_nm / dt_hours > _MAX_PLAUSIBLE_SPEED_KN:
            (implausible_startup if fix_time - session_start <= _POWER_ON_SETTLE_S else implausible_times).add(fix_time)
            continue
        dt_s = dt_hours * 3600
        if current_sog_ms is not None and dt_s <= _SOG_CHECK_MAX_DT_S:
            allowed_m = _SOG_CHECK_SLACK_M + dt_s * (_SOG_CHECK_FACTOR * current_sog_ms + _SOG_CHECK_SLACK_MS)
            if distance_nm * 1852.0 > allowed_m:
                (
                    sog_inconsistent_startup if fix_time - session_start <= _POWER_ON_SETTLE_S else sog_inconsistent_times
                ).add(fix_time)
                continue
        if fix_time - fixes.time_at(prev_i) >= _POWER_ON_GAP_S:
            session_start = fix_time
        accepted.append_raw(fix_time, fixes.lat_at(i), fixes.lon_at(i))
        prev_i = i
    if backward_dropped or clock_resets:
        _log_time_anomaly("Position fixes", backward_dropped, max_backward_s, first_backward_at, clock_resets)
    implausible_what = f"implying more than {_MAX_PLAUSIBLE_SPEED_KN:.0f} kn from the previous accepted one"
    sog_what = "that moved much further than the receiver's own speed over ground allows"
    for startup, elsewhere, what in (
        (implausible_startup, implausible_times, implausible_what),
        (sog_inconsistent_startup, sog_inconsistent_times, sog_what),
    ):
        if startup.total:
            log(
                f"[info] Position fixes: dropped {startup.total} fix(es) {what}, at {startup.describe()}, within "
                f"{_POWER_ON_SETTLE_S:.0f} s of a power-on -- the receiver still settling on its position (expected)."
            )
        if elsewhere.total:
            log(
                f"[anomaly] Position fixes: dropped {elsewhere.total} fix(es) {what}, at {elsewhere.describe()} -- "
                f"a GPS position jump or corrupted position data in the source, worth a look if it keeps happening."
            )
    # Written back into `fixes`' own columns (see FixArray.replace_columns_with's own docstring)
    # rather than simply `return accepted` -- the caller passed `fixes` in by reference and, on
    # every real caller (via pipeline.py), still holds its own separate reference to the
    # exact same object for build_trips()'s entire run; returning a distinct new FixArray would
    # leave the original, larger columns resident and unreachable-but-not-freed the whole time.
    fixes.replace_columns_with(accepted)
    return fixes


def _merge_nav_samples(
    fixes: FixArray,
    sogs: SogArray,
    depths: Optional[DepthArray] = None,
    water_temps: Optional[WaterTempArray] = None,
) -> NavSampleArray:
    """Combines position, speed, depth, and water temperature readings chronologically; all are
    forward-filled.

    fixes/sogs are FixArray/SogArray (see fix_array.py) rather than plain lists -- always, by the
    time this is called from build_trips() (the only caller), which wraps whatever it was given
    into those types up front. A season's worth of either can be millions of samples. Returns a
    NavSampleArray (see fix_array.py) rather than a List[NavSample] for the same reason: a real
    multi-year archive holds millions of merged samples, resident for build_trips()'s entire run
    (confirmed in practice as the single biggest memory cost left in the whole pipeline, see
    NavSampleArray's own docstring) -- appending raw columns here means no NavSample object ever
    gets boxed per fix at all any more; only a single trip's or stay's own, already-small selection
    of rows gets turned back into real NavSample objects later, via _materialize(). SOG in
    particular used to go through a plain sorted(sogs, key=lambda s: s.time) here, materializing a
    full season's worth of SogSample objects that then stayed resident for this whole function's
    run -- found in practice, on a real ~2 million-fix archive, this function is where the phone's
    decode-through-publish pipeline was actually dying (confirmed via the checkpoint logging
    around build_trips(), see that function's own comment): SOG is typically similar cardinality
    to position fixes, so that materialization alone was a real, multi-hundred-MB cost on top of
    everything else already resident at that point."""
    sogs = sogs.drop_time_regressions()  # first: the position check below compares against SOG
    fixes = _reject_gps_outliers_array(fixes, sogs)
    # Already in time order -- _reject_gps_outliers_array() processes its input in sorted order
    # and never reorders what it keeps, so no second sort is needed here.
    depths = (depths if depths is not None else DepthArray()).drop_time_regressions()
    water_temps = (water_temps if water_temps is not None else WaterTempArray()).drop_time_regressions()
    samples = NavSampleArray()
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
        while depth_idx < len(depths) and depths.time_at(depth_idx) <= fix_epoch:
            last_depth = depths.depth_at(depth_idx)
            depth_idx += 1
        while water_temp_idx < len(water_temps) and water_temps.time_at(water_temp_idx) <= fix_epoch:
            last_water_temp = water_temps.temp_at(water_temp_idx)
            water_temp_idx += 1
        samples.append_raw(fix_epoch, fixes.lat_at(i), fixes.lon_at(i), last_sog, last_depth, last_water_temp, last_cog)
    return samples


def _classify_runs(samples: NavSampleArray, speed_threshold_ms: float) -> List[Tuple[str, Group]]:
    n = len(samples)
    if n == 0:
        return []
    runs: List[Tuple[str, Group]] = []
    run_label = "stationary" if samples.sog_at(0) < speed_threshold_ms else "moving"
    run_start = 0
    for i in range(1, n):
        label = "stationary" if samples.sog_at(i) < speed_threshold_ms else "moving"
        if label != run_label:
            runs.append((run_label, [(run_start, i)]))
            run_label = label
            run_start = i
    runs.append((run_label, [(run_start, n)]))
    return runs


def _merge_short_stops(
    samples: NavSampleArray, runs: List[Tuple[str, Group]], min_stop: timedelta, max_gap: timedelta
) -> List[Tuple[str, Group]]:
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
        if label == "stationary" and (_group_end_time(samples, group) - _group_start_time(samples, group)) < min_stop:
            gap_before = idx > 0 and (
                _group_start_time(samples, group) - _group_end_time(samples, runs[idx - 1][1])
            ) >= max_gap
            gap_after = idx + 1 < len(runs) and (
                _group_start_time(samples, runs[idx + 1][1]) - _group_end_time(samples, group)
            ) >= max_gap
            if not (gap_before or gap_after):
                label = "moving"
        relabelled.append((label, group))

    return _merge_adjacent(relabelled)


def _merge_adjacent(runs: List[Tuple[str, Group]]) -> List[Tuple[str, Group]]:
    """Joins consecutive runs that ended up with the same label after relabelling (e.g. a
    "stationary" run just turned back into "moving") into a single run."""
    merged: List[Tuple[str, Group]] = []
    for label, group in runs:
        if merged and merged[-1][0] == label:
            merged[-1] = (label, merged[-1][1] + group)
        else:
            merged.append((label, group))
    return merged


def _spatial_spread_m(samples: NavSampleArray, group: Group) -> float:
    """Max distance (meters) from the group's centroid to any point in it."""
    total_lat = total_lon = 0.0
    count = 0
    for start, end in group:
        for i in range(start, end):
            total_lat += samples.lat_at(i)
            total_lon += samples.lon_at(i)
            count += 1
    lat = total_lat / count
    lon = total_lon / count
    return max(
        _haversine_nm(lat, lon, samples.lat_at(i), samples.lon_at(i)) * 1852.0
        for start, end in group
        for i in range(start, end)
    )


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


def _engine_on_intervals(
    engine_samples: EngineArray,
) -> Tuple[List[Tuple[datetime, datetime]], Dict[int, List[Tuple[datetime, datetime]]]]:
    """(intervals across all engine instances, intervals per engine instance) during which an
    engine was actually running, from a single pass over the samples. Based on fuel consumption --
    a much more direct "is it running" signal than merely receiving PGN 127489, since some devices
    keep sending near-zero readings for a while after shutdown. Consecutive "on" readings less
    than ``_ENGINE_OFF_GAP_S`` apart are treated as one continuous interval, bridging normal
    reporting jitter without bridging a real shutdown.

    The per-instance intervals extend a trip's own logged engine hours by however long that
    specific engine ran continuously right before departure and after arrival (see
    ``_engine_hours_delta``), without mixing in a different engine's own on/off timing on a
    multi-engine boat."""
    on_times_by_instance: Dict[int, List[datetime]] = {}
    for s in engine_samples:
        if s.fuel_rate_lph is not None and s.fuel_rate_lph > _ENGINE_IDLE_FUEL_LPH:
            on_times_by_instance.setdefault(s.instance, []).append(s.time)
    overall = _merge_on_times(sorted(t for times in on_times_by_instance.values() for t in times))
    by_instance = {instance: _merge_on_times(sorted(times)) for instance, times in on_times_by_instance.items()}
    return overall, by_instance


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
    samples: NavSampleArray,
    runs: List[Tuple[str, Group]],
    on_intervals: List[Tuple[datetime, datetime]],
    lock_radius_m: float,
    lock_max_duration: timedelta,
) -> List[Tuple[str, Group]]:
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
            span = _engine_off_span(on_intervals, _group_start_time(samples, group), _group_end_time(samples, group))
            if span is not None:
                off_start, off_end = span
                if off_end - off_start <= lock_max_duration:
                    span_range = samples.index_range_for_time(off_start, off_end)
                    span_group: Group = [span_range] if span_range else group
                    if _spatial_spread_m(samples, span_group) <= lock_radius_m:
                        label = "moving"
            elif idx + 1 < len(runs):
                if (
                    _group_end_time(samples, group) - _group_start_time(samples, group) <= lock_max_duration
                    and _spatial_spread_m(samples, group) <= lock_radius_m
                ):
                    label = "moving"
        relabelled.append((label, group))
    return relabelled


def _split_runs_on_gaps(
    samples: NavSampleArray, runs: List[Tuple[str, Group]], max_gap: timedelta
) -> List[Tuple[str, Group]]:
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
    max_gap_s = max_gap.total_seconds()
    result: List[Tuple[str, Group]] = []
    for label, group in runs:
        # At this point every group is still a single contiguous span of array indices (possibly
        # spread across more than one adjacent tuple, e.g. after _merge_adjacent) -- the splicing
        # that can make a group non-contiguous only happens later, in _merge_negligible_trips --
        # so scanning the plain index range [lo, hi) for internal gaps is enough here.
        lo, hi = group[0][0], group[-1][1]
        seg_start = lo
        for i in range(lo, hi - 1):
            if samples.time_at(i + 1) - samples.time_at(i) >= max_gap_s:
                result.append((label, [(seg_start, i + 1)]))
                if label == "moving":
                    result.append(("stationary", [(i, i + 1)]))
                seg_start = i + 1
        result.append((label, [(seg_start, hi)]))
    return result


def _trip_distance_nm(samples: NavSampleArray, group: Group) -> float:
    return sum(
        _haversine_nm(samples.lat_at(i), samples.lon_at(i), samples.lat_at(j), samples.lon_at(j))
        for i, j in _group_index_pairs(group)
    )


def _merge_negligible_trips(
    samples: NavSampleArray,
    runs: List[Tuple[str, Group]],
    min_leg_distance_nm: float,
    max_gap: timedelta,
    lock_radius_m: Optional[float] = None,
) -> List[Tuple[str, Group]]:
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
    Such a confined run is never spliced onto the preceding trip (it reaches nowhere new, so
    there is no route to extend): it just becomes part of the stay it sits in.

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
    result: List[Tuple[str, Group]] = []
    last_moving_group: Optional[Group] = None
    prev_end_time: Optional[datetime] = None
    for label, group in runs:
        if prev_end_time is not None and _group_start_time(samples, group) - prev_end_time >= max_gap:
            last_moving_group = None  # a real data gap -- never splice/merge across it
        prev_end_time = _group_end_time(samples, group)

        confined = lock_radius_m is not None and _spatial_spread_m(samples, group) <= lock_radius_m
        is_negligible = confined or _trip_distance_nm(samples, group) < min_leg_distance_nm
        if label == "moving" and is_negligible:
            # A confined run is idle-at-the-quay noise: it reaches nowhere new, so there is no
            # route to extend, and splicing it onto the earlier trip would add its whole duration
            # and path length to a trip that ended before the boat sat there (found in practice:
            # a 21-minute trip reported as 0:54 because 33 minutes of SOG noise 27 minutes after
            # arriving were appended to it).
            if last_moving_group is not None and not confined:
                last_moving_group.extend(group)
                continue
            label = "stationary"
        if (
            label == "stationary"
            and result
            and result[-1][0] == "stationary"
            and _group_start_time(samples, group) - _group_end_time(samples, result[-1][1]) < max_gap
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


class _InstanceIndex:
    """Per instance (engine/battery number) of a season-wide, array.array-backed sample collection,
    the row indices in time order -- computed once per array in build_trips(), so the per-trip
    "leaf" functions below can bisect each instance's own small time window out of it (see
    ``window``) instead of re-scanning the *entire* season's samples from scratch on every call.
    Found in practice: once the earlier OOM/sort issues were fixed, that re-scanning was the next
    bottleneck -- a single trip could take minutes with ~400k engine + ~1.75M RPM samples each
    re-scanned by 6+ separate functions.

    Holds only row *indices* (a few bytes each), not sample objects: materializing every one of a
    season's ~2 million engine/RPM/battery samples as real Python objects for the whole run is
    exactly the cost the columnar arrays exist to avoid. Only a trip's own window is ever turned
    into objects, and only for the duration of that trip's statistics."""

    def __init__(self, samples) -> None:
        self._samples = samples
        self._rows: Dict[int, "array.array[int]"] = {}
        for i in range(len(samples)):
            self._rows.setdefault(samples.instance_at(i), array.array("l")).append(i)
        for instance, rows in self._rows.items():
            times = [samples.time_at(i) for i in rows]
            if any(a > b for a, b in zip(times, times[1:])):
                self._rows[instance] = array.array("l", sorted(rows, key=samples.time_at))

    def instances(self) -> List[int]:
        return list(self._rows)

    def window(self, instance: int, start: datetime, end: datetime) -> list:
        """The instance's samples with start <= time <= end, in time order."""
        rows = self._rows[instance]
        lo = bisect.bisect_left(rows, _to_epoch(start), key=self._samples.time_at)
        hi = bisect.bisect_right(rows, _to_epoch(end), key=self._samples.time_at)
        return [self._samples[i] for i in rows[lo:hi]]


def _engine_hours_delta(
    engine_by_instance: Dict[int, List[EngineSample]],
    depart_time: datetime,
    arrive_time: datetime,
    prev_stay: Optional[Stay],
    next_stay: Optional[Stay],
    on_intervals_by_instance: Dict[int, List[Tuple[datetime, datetime]]],
) -> Dict[int, float]:
    """Hours the engine's own hour meter advanced during the trip, per engine instance -- with
    the window widened per instance first (see ``_extend_engine_window``): each instance gets its
    own window since a multi-engine boat's engines don't necessarily start/stop together."""
    result: Dict[int, float] = {}
    for instance in engine_by_instance.instances():
        start, end = _extend_engine_window(
            depart_time, arrive_time, prev_stay, next_stay, on_intervals_by_instance.get(instance, [])
        )
        window = [s for s in engine_by_instance.window(instance, start, end) if s.total_hours_s is not None]
        if len(window) < 2:
            continue
        delta_s = window[-1].total_hours_s - window[0].total_hours_s
        result[instance] = max(delta_s, 0) / 3600.0
    return result


def _engine_hours_total(by_instance: Dict[int, List[EngineSample]], start: datetime, end: datetime) -> Dict[int, float]:
    """Absolute engine-hour-meter reading (not a delta) at the end of the window, per engine
    instance -- the engine's own lifetime counter, e.g. for tracking maintenance intervals,
    as opposed to ``_engine_hours_delta``'s "hours run just during this trip"."""
    result: Dict[int, float] = {}
    for instance in by_instance.instances():
        window = [s for s in by_instance.window(instance, start, end) if s.total_hours_s is not None]
        if not window:
            continue
        result[instance] = window[-1].total_hours_s / 3600.0
    return result


def _fuel_liters(by_instance: Dict[int, List[EngineSample]], start: datetime, end: datetime) -> float:
    total = 0.0
    for instance in by_instance.instances():
        window = [s for s in by_instance.window(instance, start, end) if s.fuel_rate_lph is not None]
        for a, b in zip(window, window[1:]):
            dt_h = (b.time - a.time).total_seconds() / 3600.0
            if dt_h <= 0 or dt_h > _MAX_INTEGRATION_GAP_H:
                continue
            total += dt_h * (a.fuel_rate_lph + b.fuel_rate_lph) / 2
    return total


def _device_fuel_delta(
    by_instance: Dict[int, List[TripFuelSample]], start: datetime, end: datetime
) -> Optional[float]:
    """Difference between the start and end reading of the engine's own trip meter within the
    time window.

    Returns None if this PGN wasn't (sufficiently) available for this trip -- e.g. because the
    device doesn't send it -- instead of a misleading 0.
    """
    total = 0.0
    found_any = False
    for instance in by_instance.instances():
        window = [s for s in by_instance.window(instance, start, end) if s.trip_fuel_used_l is not None]
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
    by_instance: Dict[int, List[EngineSample]], start: datetime, end: datetime
) -> Dict[int, EngineHealth]:
    def _avg(values: List[float]) -> Optional[float]:
        return sum(values) / len(values) if values else None

    result: Dict[int, EngineHealth] = {}
    for instance in by_instance.instances():
        window = by_instance.window(instance, start, end)
        if not window:
            continue
        oil_pressure = [s.oil_pressure_pa for s in window if s.oil_pressure_pa is not None]
        oil_temperature = [s.oil_temperature_k for s in window if s.oil_temperature_k is not None]
        coolant_temperature = [s.coolant_temperature_k for s in window if s.coolant_temperature_k is not None]
        alternator_voltage = [s.alternator_voltage_v for s in window if s.alternator_voltage_v is not None]
        engine_load = [s.engine_load_pct for s in window if s.engine_load_pct is not None]
        warnings: FrozenSet[str] = frozenset().union(*(s.warnings for s in window))
        warning_first_seen: Dict[str, datetime] = {}
        # window is already time-sorted (see _InstanceIndex.window) -- no need
        # to re-sort it again just for this.
        for sample in window:
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


def _battery_health(by_instance: Dict[int, List[BatterySample]], start: datetime, end: datetime) -> Dict[int, BatteryHealth]:
    result: Dict[int, BatteryHealth] = {}
    for instance in by_instance.instances():
        window = [s for s in by_instance.window(instance, start, end) if s.voltage_v is not None]
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
    rpm_by_instance: Dict[int, List[EngineRpmSample]], time: datetime, start: datetime, end: datetime
) -> Dict[int, float]:
    """The RPM reading closest to ``time`` (e.g. the moment of the trip's max speed), per engine
    instance -- restricted to this trip's own [start, end] window so a gap in RPM reporting right
    at that moment doesn't pick up a reading that actually belongs to a different trip."""
    result: Dict[int, float] = {}
    for instance in rpm_by_instance.instances():
        window = [s for s in rpm_by_instance.window(instance, start, end) if s.rpm is not None]
        if not window:
            continue
        result[instance] = min(window, key=lambda s: abs((s.time - time).total_seconds())).rpm
    return result


def _typical_rpm(rpm_by_instance: Dict[int, List[EngineRpmSample]], start: datetime, end: datetime) -> Dict[int, float]:
    """The most commonly occurring engine speed (RPM) during the trip, per engine instance --
    rounded to the nearest ``_RPM_BUCKET`` before counting, so normal small load fluctuations at
    a steady cruising speed don't get spread across too many distinct exact values to ever "win".
    This is a more representative "cruising RPM" than an average (skewed by idle/neutral periods
    and maneuvering) or a maximum (skewed by brief revs)."""
    result: Dict[int, float] = {}
    for instance in rpm_by_instance.instances():
        values = [s.rpm for s in rpm_by_instance.window(instance, start, end) if s.rpm is not None]
        if not values:
            continue
        buckets = Counter(round(v / _RPM_BUCKET) * _RPM_BUCKET for v in values)
        result[instance] = float(buckets.most_common(1)[0][0])
    return result


def _typical_rpm_speed_range(
    rpm_by_instance: Dict[int, List[EngineRpmSample]],
    engine_by_instance: Dict[int, List[EngineSample]],
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

    by_instance: Dict[int, List[EngineRpmSample]] = {
        instance: [s for s in rpm_by_instance.window(instance, start, end) if s.rpm is not None]
        for instance in rpm_by_instance.instances()
    }

    fuel_by_instance: Dict[int, List[EngineSample]] = {
        instance: [s for s in engine_by_instance.window(instance, start, end) if s.fuel_rate_lph is not None]
        for instance in engine_by_instance.instances()
    }

    min_duration = timedelta(minutes=_RPM_STABLE_MINUTES)
    result: Dict[int, Tuple[float, float, float, Optional[float]]] = {}
    for instance, samples in by_instance.items():
        # Already time-sorted (see _InstanceIndex.window) -- no need to
        # re-sort it again just for this.
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


def _as_array(samples, array_type):
    """``samples`` itself if it already is an ``array_type``, otherwise a new one built from it
    (None means empty)."""
    if isinstance(samples, array_type):
        return samples
    return array_type(samples if samples is not None else ())


def build_trips(
    fixes: Union[FixArray, Iterable[PositionFix]],
    sogs: Union[SogArray, Iterable[SogSample]],
    engine_samples: Union[EngineArray, Iterable[EngineSample]],
    trip_fuel_samples: Optional[Union[TripFuelArray, Iterable[TripFuelSample]]] = None,
    depth_samples: Optional[Union[DepthArray, Iterable[DepthSample]]] = None,
    water_temp_samples: Optional[Union[WaterTempArray, Iterable[WaterTempSample]]] = None,
    battery_samples: Optional[Union[BatteryArray, Iterable[BatterySample]]] = None,
    rpm_samples: Optional[Union[RpmArray, Iterable[EngineRpmSample]]] = None,
    attitude_samples: Optional[Union[AttitudeArray, Iterable[AttitudeSample]]] = None,
    *,
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
    # Callers that already accumulate season-wide data as one of fix_array.py's array.array-backed
    # types (pipeline.py does, to avoid ever holding millions of boxed sample objects at once) pass
    # those straight through; anything else (a plain list, as every existing test in this file
    # still constructs) is wrapped here so this function's own public contract doesn't change for
    # any existing caller. Every input ends up the same kind of array, so nothing below has to
    # care which it got.
    fixes = _as_array(fixes, FixArray)
    sogs = _as_array(sogs, SogArray)
    engine_samples = _as_array(engine_samples, EngineArray)
    trip_fuel_samples = _as_array(trip_fuel_samples, TripFuelArray)
    depth_samples = _as_array(depth_samples, DepthArray)
    water_temp_samples = _as_array(water_temp_samples, WaterTempArray)
    battery_samples = _as_array(battery_samples, BatteryArray)
    rpm_samples = _as_array(rpm_samples, RpmArray)
    # Sorted once up front, in place (see _SortableSampleArray.drop_time_regressions): a plain
    # sorted(...) would iterate it into a fully-materialized list of AttitudeSample objects that
    # then lives for the rest of this function's run (passed to _motion_variation for every
    # trip), silently undoing the point of storing a season's worth of them as array.array columns.
    attitude_samples = _as_array(attitude_samples, AttitudeArray).drop_time_regressions()
    if max_gap_minutes is None:
        max_gap_minutes = min_stop_minutes

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
    on_intervals, on_intervals_by_instance = _engine_on_intervals(engine_samples)

    # Bucketed+sorted once here rather than inside each per-trip "leaf" function below (see
    # _InstanceIndex) -- otherwise every one of them re-scans the *entire* season's engine/
    # RPM/battery/trip-fuel samples from scratch, once per trip, which is exactly what made this
    # phase slow in practice once the earlier OOM/sort issues were fixed (found in practice: a
    # single trip taking minutes with ~400k engine + ~1.75M RPM samples for a full season).
    engine_by_instance = _InstanceIndex(engine_samples)
    rpm_by_instance = _InstanceIndex(rpm_samples)
    battery_by_instance = _InstanceIndex(battery_samples)
    trip_fuel_by_instance = _InstanceIndex(trip_fuel_samples)

    runs = _classify_runs(samples, speed_threshold_ms)
    runs = _merge_short_stops(samples, runs, min_stop, max_gap)
    if lock_radius_m is not None and lock_max_duration_minutes is not None:
        lock_max_duration = timedelta(minutes=lock_max_duration_minutes)
        runs = _reclassify_locks(samples, runs, on_intervals, lock_radius_m, lock_max_duration)
        runs = _merge_adjacent(runs)
    runs = _split_runs_on_gaps(samples, runs, max_gap)
    effective_min_leg_distance_nm = (
        min_leg_distance_nm if min_leg_distance_nm is not None else min_trip_distance_nm
    )
    runs = _merge_negligible_trips(samples, runs, effective_min_leg_distance_nm, max_gap, lock_radius_m)
    log(f"[info] ...{len(runs)} run(s) classified, computing per-trip statistics...")

    # A cheap, offline placeholder -- never the real geocoder. Actually resolving place names
    # here would get them baked straight into a "settled" TripLeg (see trip_cache.py), frozen in
    # the cache forever the moment this trip stops being freshly rebuilt every run: a real
    # lookup failure (a rate-limited geocoding service, no internet that one time, ...) would
    # otherwise permanently stick a wrong/degraded name on that trip, with no way for a later,
    # working run to ever correct it short of clearing the whole trip cache and re-decoding
    # everything (found in practice). resolve_trip_places() below is the real, always-rerun
    # resolution step instead -- run once, every run, over every trip (settled or fresh alike),
    # right before a trip is actually shown/written -- so a fixed connection or an unrelated
    # geocode-cache clear fixes it on the very next run, no full rebuild required.
    placeholder_geocoder = NoGeocoder()
    stays: List[Optional[Stay]] = []
    total_stays = sum(1 for label, _ in runs if label == "stationary")
    stays_done = 0
    for label, group in runs:
        if label != "stationary":
            stays.append(None)
            continue
        track = _materialize(samples, group)
        lat, lon = _settled_position(track, speed_threshold_ms, on_intervals, lock_radius_m)
        place = placeholder_geocoder.place_name(lat, lon)
        stays.append(Stay(track[0].time, track[-1].time, lat, lon, place))
        stays_done += 1
        log(f"[info] ......stay {stays_done}/{total_stays} processed")

    trips: List[TripLeg] = []
    total_trips = sum(1 for label, _ in runs if label == "moving")
    trips_done = 0
    for idx, (label, group) in enumerate(runs):
        if label != "moving":
            continue
        prev_stay = stays[idx - 1] if idx > 0 else None
        next_stay = stays[idx + 1] if idx + 1 < len(stays) else None

        track = _materialize(samples, group)

        depart_time = prev_stay.end if prev_stay else track[0].time
        if prev_stay is not None and track[0].time - depart_time >= max_gap:
            # Mirror image of the arrival case below: the logger was off for a real data gap
            # between the last sample of the previous stay and the first sample of this trip, so
            # the boat was only *seen* leaving when data resumed. The previous stay still supplies
            # the departure *place*, but not a departure time from before the gap. Found in
            # practice: a trip reported as departing at 08:56 local, when the logger only came
            # back on at 10:21 and the boat moved from there.
            depart_time = track[0].time
        arrive_time = next_stay.start if next_stay else track[-1].time
        if next_stay is not None and arrive_time - track[-1].time >= max_gap:
            # The next stay only starts after a real data gap (e.g. the logger was switched off
            # right after arriving): it still supplies the arrival *place* (ports are linked across
            # a gap, see _merge_short_stops), but the boat was last seen at the end of this track,
            # not hours later when data resumed. Found in practice: a trip that ended at 12:14
            # local was reported as arriving at 14:39, when the logger came back on.
            arrive_time = track[-1].time
        depart_place = prev_stay.place if prev_stay else "Unknown (start outside log file)"
        arrive_place = next_stay.place if next_stay else "Unknown (end outside log file)"
        depart_lat = prev_stay.lat if prev_stay else track[0].lat
        depart_lon = prev_stay.lon if prev_stay else track[0].lon
        arrive_lat = next_stay.lat if next_stay else track[-1].lat
        arrive_lon = next_stay.lon if next_stay else track[-1].lon

        distance_nm = _trip_distance_nm(samples, group)
        avg_speed_kn, max_speed_kn, max_speed_at = _speed_stats_kn(track)
        max_speed_rpm = (
            _rpm_at_time(rpm_by_instance, max_speed_at, depart_time, arrive_time) if max_speed_at else {}
        )
        min_depth_m, min_depth_lat, min_depth_lon = _min_depth(track)
        avg_water_temp_c, min_water_temp_c, max_water_temp_c = _water_temp_stats(track)
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
                duration=_moving_duration(track, max_gap),
                distance_nm=distance_nm,
                avg_speed_kn=avg_speed_kn,
                max_speed_kn=max_speed_kn,
                fuel_liters=_fuel_liters(engine_by_instance, depart_time, arrive_time),
                fuel_liters_device=_device_fuel_delta(trip_fuel_by_instance, depart_time, arrive_time),
                engine_hours=_engine_hours_delta(
                    engine_by_instance, depart_time, arrive_time, prev_stay, next_stay, on_intervals_by_instance
                ),
                engine_hours_total=_engine_hours_total(engine_by_instance, depart_time, arrive_time),
                engine_health=_engine_health(engine_by_instance, depart_time, arrive_time),
                typical_rpm=_typical_rpm(rpm_by_instance, depart_time, arrive_time),
                typical_rpm_speed_kn=_typical_rpm_speed_range(
                    rpm_by_instance, engine_by_instance, track, depart_time, arrive_time
                ),
                battery_health=_battery_health(battery_by_instance, depart_time, arrive_time),
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
                    track, depart_time, depart_lat, depart_lon, arrive_time, arrive_lat, arrive_lon
                ),
                max_speed_at=max_speed_at,
                max_speed_rpm=max_speed_rpm,
            )
        )
        trips_done += 1
        log(f"[info] ......trip {trips_done}/{total_trips} processed")
    return [trip for trip in trips if trip.distance_nm >= min_trip_distance_nm]


def resolve_trip_places(trips: List[TripLeg], geocoder: object) -> List[TripLeg]:
    """Fills in depart_place/arrive_place with real place names (asked for explicitly, moved out
    of build_trips() itself -- see its own placeholder_geocoder comment for the full reasoning):
    the trip's own geometry/timing/statistics genuinely never change once settled, so caching
    those is exactly right, but a place name can still improve later (a rate-limited geocoding
    service recovering, a stale entry in the geocode cache getting cleared, ...) -- there's no
    good reason a caller should ever need a full trip-cache clear and re-decode just to pick that
    up. Callers should run this over *every* trip about to be shown/written (settled trips loaded
    straight from trip_cache.pkl included, not just newly-built ones) on every run: the geocoder's
    own cache (see geocode.Geocoder) already makes a repeat, already-successful lookup cheap, so
    this doesn't reintroduce the network cost settling was there to avoid -- only a lookup that
    previously failed (and so was never cached, see Geocoder._lookup's own cacheable return)
    actually costs anything here, exactly the case worth retrying.

    "Unknown (start/end outside log file)" placeholders (a trip with no known stay on that end,
    see build_trips() above) are left alone -- there's no position to look up in the first place,
    and geocoding wasn't ever involved in producing that text."""
    resolved: List[TripLeg] = []
    for trip in trips:
        depart_place = trip.depart_place
        if not depart_place.startswith("Unknown ("):
            depart_place = geocoder.place_name(trip.depart_lat, trip.depart_lon)
        arrive_place = trip.arrive_place
        if not arrive_place.startswith("Unknown ("):
            arrive_place = geocoder.place_name(trip.arrive_lat, trip.arrive_lon)
        if depart_place == trip.depart_place and arrive_place == trip.arrive_place:
            resolved.append(trip)  # no change -- skip the copy
        else:
            resolved.append(replace(trip, depart_place=depart_place, arrive_place=arrive_place))
    return resolved
