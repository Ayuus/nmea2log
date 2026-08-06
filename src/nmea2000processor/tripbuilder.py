"""Turns sequences of positions/speeds/engine data into logbook trips (departure port ->
arrival port).

Approach: every GPS fix is classified as 'stationary' or 'underway' based on speed over
ground. Consecutive stationary periods that last long enough (threshold ``min_stop_minutes``)
are considered a port visit; the periods in between are the trips. For each trip, fuel
consumption is calculated by integrating the fuel-rate readings (PGN 127489, engine data) over
time -- so explicitly not via a tank sensor.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby
from typing import Dict, FrozenSet, List, Optional, Tuple

from .geocode import Geocoder, NoGeocoder
from .model import (
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


@dataclass(frozen=True)
class NavSample:
    time: datetime
    lat: float
    lon: float
    sog_ms: float
    depth_m: Optional[float] = None
    water_temp_c: Optional[float] = None


@dataclass(frozen=True)
class Stay:
    start: datetime
    end: datetime
    lat: float
    lon: float
    place: str


@dataclass(frozen=True)
class EngineHealth:
    oil_pressure_bar_avg: Optional[float]
    oil_temperature_c_avg: Optional[float]
    coolant_temperature_c_avg: Optional[float]
    alternator_voltage_v_avg: Optional[float]
    engine_load_pct_max: Optional[float]
    warnings: FrozenSet[str]


@dataclass(frozen=True)
class BatteryHealth:
    avg_voltage_v: Optional[float]
    min_voltage_v: Optional[float]


@dataclass(frozen=True)
class TripLeg:
    depart_time: datetime
    arrive_time: datetime
    depart_place: str
    arrive_place: str
    duration: timedelta  # time underway, excluding any gaps in the data (see _moving_duration)
    distance_nm: float
    avg_speed_kn: Optional[float]
    max_speed_kn: Optional[float]
    fuel_liters: float  # calculated by integrating the fuel rate (PGN 127489) over time
    fuel_liters_device: Optional[float]  # engine's own trip meter (PGN 127497), None = not available
    engine_hours: Dict[int, float]  # engine instance -> hours run during this trip
    engine_hours_total: Dict[int, float]  # engine instance -> absolute hour-meter reading at arrival
    engine_health: Dict[int, EngineHealth]  # engine instance -> health indicators + warnings
    typical_rpm: Dict[int, float]  # engine instance -> most commonly occurring RPM during the trip
    battery_health: Dict[int, BatteryHealth]  # battery instance -> voltage stats during this trip
    min_depth_m: Optional[float]  # shallowest water depth measured during this trip
    min_depth_lat: Optional[float]
    min_depth_lon: Optional[float]
    avg_water_temp_c: Optional[float]
    min_water_temp_c: Optional[float]
    max_water_temp_c: Optional[float]
    track: List[NavSample]  # GPS points of this trip, e.g. for GPX export


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_NM * math.asin(math.sqrt(a))


def _merge_nav_samples(
    fixes: List[PositionFix],
    sogs: List[SogSample],
    depths: Optional[List[DepthSample]] = None,
    water_temps: Optional[List[WaterTempSample]] = None,
) -> List[NavSample]:
    """Combines position, speed, depth, and water temperature readings chronologically; all are
    forward-filled."""
    sogs_sorted = sorted(sogs, key=lambda s: s.time)
    depths_sorted = sorted(depths, key=lambda s: s.time) if depths else []
    water_temps_sorted = sorted(water_temps, key=lambda s: s.time) if water_temps else []
    samples: List[NavSample] = []
    sog_idx = 0
    depth_idx = 0
    water_temp_idx = 0
    last_sog = 0.0
    last_depth: Optional[float] = None
    last_water_temp: Optional[float] = None
    for fix in sorted(fixes, key=lambda f: f.time):
        while sog_idx < len(sogs_sorted) and sogs_sorted[sog_idx].time <= fix.time:
            last_sog = sogs_sorted[sog_idx].sog_ms
            sog_idx += 1
        while depth_idx < len(depths_sorted) and depths_sorted[depth_idx].time <= fix.time:
            last_depth = depths_sorted[depth_idx].depth_m
            depth_idx += 1
        while water_temp_idx < len(water_temps_sorted) and water_temps_sorted[water_temp_idx].time <= fix.time:
            last_water_temp = water_temps_sorted[water_temp_idx].temp_c
            water_temp_idx += 1
        samples.append(NavSample(fix.time, fix.lat, fix.lon, last_sog, last_depth, last_water_temp))
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


def _engine_on_intervals(engine_samples: List[EngineSample]) -> List[Tuple[datetime, datetime]]:
    """Merged time ranges (across all engine instances) during which an engine was actually
    running, based on fuel consumption -- a much more direct "is it running" signal than merely
    receiving PGN 127489, since some devices keep sending near-zero readings for a while after
    shutdown. Consecutive "on" readings less than ``_ENGINE_OFF_GAP_S`` apart are treated as one
    continuous interval, bridging normal reporting jitter without bridging a real shutdown."""
    on_times = sorted(
        s.time for s in engine_samples if s.fuel_rate_lph is not None and s.fuel_rate_lph > _ENGINE_IDLE_FUEL_LPH
    )
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
    actually is, which is not available without unreliable external geocoding."""
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
        relabelled.append((label, group))
    return relabelled


def _split_moving_runs_on_gaps(
    runs: List[Tuple[str, List[NavSample]]], max_gap: timedelta
) -> List[Tuple[str, List[NavSample]]]:
    """A "moving" run can silently swallow a large data gap if the classification happens to be
    "moving" on both sides of it (e.g. a moment of GPS/SOG noise right as the boat was actually
    stopping, and again once data resumes) -- there's then no differently-labeled sample in
    between for ``_classify_runs`` to split on, even though we have no idea what happened during
    the gap. Found in practice: a trip that looked like one continuous ~4-hour "moving" run
    actually had a ~2.5-hour data gap in the middle, right after the boat had actually arrived;
    ``_moving_duration`` already excluded the gap from the reported *duration* correctly, but the
    arrival itself was never recognized as a stay, so the CSV showed the *next* real stay (found
    hours later) as the arrival port instead of the true one.

    This splits a "moving" run at each internal gap >= ``max_gap``, inserting a single-sample
    synthetic "stationary" stay at the last known position before the gap -- the same treatment
    as an unresolved position at a file/session boundary, just discovered mid-run instead of at
    the edges."""
    result: List[Tuple[str, List[NavSample]]] = []
    for label, group in runs:
        if label != "moving":
            result.append((label, group))
            continue
        segment: List[NavSample] = [group[0]]
        for prev, curr in zip(group, group[1:]):
            if curr.time - prev.time >= max_gap:
                result.append(("moving", segment))
                result.append(("stationary", [prev]))
                segment = [curr]
            else:
                segment.append(curr)
        result.append(("moving", segment))
    return result


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


def _speed_stats_kn(track: List[NavSample]) -> Tuple[Optional[float], Optional[float]]:
    if not track:
        return None, None
    speeds_kn = [s.sog_ms / _KNOT_IN_MS for s in track]
    return sum(speeds_kn) / len(speeds_kn), max(speeds_kn)


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
        window = [s.voltage_v for s in seq if start <= s.time <= end]
        if not window:
            continue
        result[instance] = BatteryHealth(avg_voltage_v=sum(window) / len(window), min_voltage_v=min(window))
    return result


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


def build_trips(
    fixes: List[PositionFix],
    sogs: List[SogSample],
    engine_samples: List[EngineSample],
    trip_fuel_samples: Optional[List[TripFuelSample]] = None,
    depth_samples: Optional[List[DepthSample]] = None,
    water_temp_samples: Optional[List[WaterTempSample]] = None,
    battery_samples: Optional[List[BatterySample]] = None,
    rpm_samples: Optional[List[EngineRpmSample]] = None,
    *,
    geocoder: Optional[object] = None,
    speed_threshold_kn: float = 0.5,
    min_stop_minutes: float = 10.0,
    max_gap_minutes: Optional[float] = None,
    min_trip_distance_nm: float = 0.1,
    lock_radius_m: Optional[float] = None,
    lock_max_duration_minutes: Optional[float] = None,
) -> List[TripLeg]:
    """``max_gap_minutes``: how long there can be no data at most before a trip's reported
    duration gets cut off (see ``_moving_duration``). Defaults to the same value as
    ``min_stop_minutes`` -- the same number, but two different meanings: one is "how long do
    you have to be stationary", the other "how long can there be no data". Ports are still
    linked across such a gap (see ``_merge_short_stops``) -- only the trip's *duration* ignores
    the gap, not the departure/arrival port itself.

    ``min_trip_distance_nm``: trips covering less than this are filtered out. This is
    GPS/speed noise (a few seconds just above ``speed_threshold_kn``), not a real trip (found
    in practice: 0.0 nm, lasting a few seconds to minutes, engine off).

    ``lock_radius_m`` / ``lock_max_duration_minutes``: a stop is treated as a lock/bridge rather
    than a port visit if the engine was off no longer than ``lock_max_duration_minutes`` and the
    boat stayed within ``lock_radius_m`` of its own position the whole time (see
    ``_reclassify_locks``). Both default to ``None`` (disabled) at this level -- the CLI turns
    this on with sensible defaults; left off here so callers/tests that don't care about it get
    the plain speed-based behavior."""
    if geocoder is None:
        geocoder = NoGeocoder()
    if trip_fuel_samples is None:
        trip_fuel_samples = []
    if battery_samples is None:
        battery_samples = []
    if rpm_samples is None:
        rpm_samples = []
    if max_gap_minutes is None:
        max_gap_minutes = min_stop_minutes

    samples = _merge_nav_samples(fixes, sogs, depth_samples, water_temp_samples)
    if len(samples) < 2:
        return []

    speed_threshold_ms = speed_threshold_kn * _KNOT_IN_MS
    min_stop = timedelta(minutes=min_stop_minutes)
    max_gap = timedelta(minutes=max_gap_minutes)

    runs = _classify_runs(samples, speed_threshold_ms)
    runs = _merge_short_stops(runs, min_stop, max_gap)
    if lock_radius_m is not None and lock_max_duration_minutes is not None:
        on_intervals = _engine_on_intervals(engine_samples)
        lock_max_duration = timedelta(minutes=lock_max_duration_minutes)
        runs = _reclassify_locks(runs, samples, on_intervals, lock_radius_m, lock_max_duration)
        runs = _merge_adjacent(runs)
    runs = _split_moving_runs_on_gaps(runs, max_gap)

    stays: List[Optional[Stay]] = []
    for label, group in runs:
        if label != "stationary":
            stays.append(None)
            continue
        lat = sum(s.lat for s in group) / len(group)
        lon = sum(s.lon for s in group) / len(group)
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

        distance_nm = sum(
            _haversine_nm(a.lat, a.lon, b.lat, b.lon) for a, b in zip(group, group[1:])
        )
        avg_speed_kn, max_speed_kn = _speed_stats_kn(group)
        min_depth_m, min_depth_lat, min_depth_lon = _min_depth(group)
        avg_water_temp_c, min_water_temp_c, max_water_temp_c = _water_temp_stats(group)

        trips.append(
            TripLeg(
                depart_time=depart_time,
                arrive_time=arrive_time,
                depart_place=depart_place,
                arrive_place=arrive_place,
                duration=_moving_duration(group, max_gap),
                distance_nm=distance_nm,
                avg_speed_kn=avg_speed_kn,
                max_speed_kn=max_speed_kn,
                fuel_liters=_fuel_liters(engine_samples, depart_time, arrive_time),
                fuel_liters_device=_device_fuel_delta(trip_fuel_samples, depart_time, arrive_time),
                engine_hours=_engine_hours_delta(engine_samples, depart_time, arrive_time),
                engine_hours_total=_engine_hours_total(engine_samples, depart_time, arrive_time),
                engine_health=_engine_health(engine_samples, depart_time, arrive_time),
                typical_rpm=_typical_rpm(rpm_samples, depart_time, arrive_time),
                battery_health=_battery_health(battery_samples, depart_time, arrive_time),
                min_depth_m=min_depth_m,
                min_depth_lat=min_depth_lat,
                min_depth_lon=min_depth_lon,
                avg_water_temp_c=avg_water_temp_c,
                min_water_temp_c=min_water_temp_c,
                max_water_temp_c=max_water_temp_c,
                track=group,
            )
        )
    return [trip for trip in trips if trip.distance_nm >= min_trip_distance_nm]
