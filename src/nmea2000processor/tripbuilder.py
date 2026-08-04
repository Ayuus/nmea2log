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
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby
from typing import Dict, FrozenSet, List, Optional, Tuple

from .geocode import Geocoder, NoGeocoder
from .model import DepthSample, EngineSample, PositionFix, SogSample, TripFuelSample

_KNOT_IN_MS = 0.514444
_EARTH_RADIUS_NM = 3440.065
_MAX_INTEGRATION_GAP_H = 1.0  # larger gaps between engine samples indicate a log interruption
_KELVIN_TO_CELSIUS = 273.15
_PA_TO_BAR = 1e-5


@dataclass(frozen=True)
class NavSample:
    time: datetime
    lat: float
    lon: float
    sog_ms: float
    depth_m: Optional[float] = None


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
    min_depth_m: Optional[float]  # shallowest water depth measured during this trip
    min_depth_lat: Optional[float]
    min_depth_lon: Optional[float]
    track: List[NavSample]  # GPS points of this trip, e.g. for GPX export


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_NM * math.asin(math.sqrt(a))


def _merge_nav_samples(
    fixes: List[PositionFix], sogs: List[SogSample], depths: Optional[List[DepthSample]] = None
) -> List[NavSample]:
    """Combines position, speed, and depth readings chronologically; both are forward-filled."""
    sogs_sorted = sorted(sogs, key=lambda s: s.time)
    depths_sorted = sorted(depths, key=lambda s: s.time) if depths else []
    samples: List[NavSample] = []
    sog_idx = 0
    depth_idx = 0
    last_sog = 0.0
    last_depth: Optional[float] = None
    for fix in sorted(fixes, key=lambda f: f.time):
        while sog_idx < len(sogs_sorted) and sogs_sorted[sog_idx].time <= fix.time:
            last_sog = sogs_sorted[sog_idx].sog_ms
            sog_idx += 1
        while depth_idx < len(depths_sorted) and depths_sorted[depth_idx].time <= fix.time:
            last_depth = depths_sorted[depth_idx].depth_m
            depth_idx += 1
        samples.append(NavSample(fix.time, fix.lat, fix.lon, last_sog, last_depth))
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

    merged: List[Tuple[str, List[NavSample]]] = []
    for label, group in relabelled:
        if merged and merged[-1][0] == label:
            merged[-1] = (label, merged[-1][1] + group)
        else:
            merged.append((label, group))
    return merged


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


def build_trips(
    fixes: List[PositionFix],
    sogs: List[SogSample],
    engine_samples: List[EngineSample],
    trip_fuel_samples: Optional[List[TripFuelSample]] = None,
    depth_samples: Optional[List[DepthSample]] = None,
    *,
    geocoder: Optional[object] = None,
    speed_threshold_kn: float = 0.5,
    min_stop_minutes: float = 10.0,
    max_gap_minutes: Optional[float] = None,
    min_trip_distance_nm: float = 0.1,
) -> List[TripLeg]:
    """``max_gap_minutes``: how long there can be no data at most before a trip's reported
    duration gets cut off (see ``_moving_duration``). Defaults to the same value as
    ``min_stop_minutes`` -- the same number, but two different meanings: one is "how long do
    you have to be stationary", the other "how long can there be no data". Ports are still
    linked across such a gap (see ``_merge_short_stops``) -- only the trip's *duration* ignores
    the gap, not the departure/arrival port itself.

    ``min_trip_distance_nm``: trips covering less than this are filtered out. This is
    GPS/speed noise (a few seconds just above ``speed_threshold_kn``), not a real trip (found
    in practice: 0.0 nm, lasting a few seconds to minutes, engine off)."""
    if geocoder is None:
        geocoder = NoGeocoder()
    if trip_fuel_samples is None:
        trip_fuel_samples = []
    if max_gap_minutes is None:
        max_gap_minutes = min_stop_minutes

    samples = _merge_nav_samples(fixes, sogs, depth_samples)
    if len(samples) < 2:
        return []

    speed_threshold_ms = speed_threshold_kn * _KNOT_IN_MS
    min_stop = timedelta(minutes=min_stop_minutes)
    max_gap = timedelta(minutes=max_gap_minutes)

    runs = _classify_runs(samples, speed_threshold_ms)
    runs = _merge_short_stops(runs, min_stop, max_gap)
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
                min_depth_m=min_depth_m,
                min_depth_lat=min_depth_lat,
                min_depth_lon=min_depth_lon,
                track=group,
            )
        )
    return [trip for trip in trips if trip.distance_nm >= min_trip_distance_nm]
