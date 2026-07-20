"""Zet reeksen posities/snelheden/motordata om in logboek-reizen (vertrekhaven -> aankomsthaven).

Aanpak: elke GPS-fix wordt geclassificeerd als 'stilliggend' of 'varend' op basis van de
snelheid over de grond. Aaneengesloten stilligperiodes die lang genoeg duren (drempel
``min_stop_minutes``) worden beschouwd als een havenbezoek; de periodes daartussen zijn de
reizen. Voor elke reis wordt het brandstofverbruik berekend door de brandstofdebiet-metingen
(PGN 127489, motordata) te integreren over de tijd — dus expliciet niet via een tanksensor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby
from typing import Dict, List, Optional, Tuple

from .geocode import Geocoder, NoGeocoder
from .model import EngineSample, PositionFix, SogSample, TripFuelSample

_KNOT_IN_MS = 0.514444
_EARTH_RADIUS_NM = 3440.065
_MAX_INTEGRATION_GAP_H = 1.0  # grotere gaten tussen motorsamples wijzen op een logonderbreking


@dataclass(frozen=True)
class NavSample:
    time: datetime
    lat: float
    lon: float
    sog_ms: float


@dataclass(frozen=True)
class Stay:
    start: datetime
    end: datetime
    lat: float
    lon: float
    place: str


@dataclass(frozen=True)
class TripLeg:
    depart_time: datetime
    arrive_time: datetime
    depart_place: str
    arrive_place: str
    distance_nm: float
    fuel_liters: float  # berekend door brandstofdebiet (PGN 127489) te integreren over de tijd
    fuel_liters_device: Optional[float]  # motor-eigen triptmeter (PGN 127497), None = niet beschikbaar
    engine_hours: Dict[int, float]  # motor-instance -> gedraaide uren tijdens deze reis
    track: List[NavSample]  # GPS-punten van deze reis, voor bv. GPX-export


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_NM * math.asin(math.sqrt(a))


def _merge_nav_samples(fixes: List[PositionFix], sogs: List[SogSample]) -> List[NavSample]:
    """Combineert positie- en snelheidsmetingen chronologisch; snelheid wordt forward-filled."""
    sogs_sorted = sorted(sogs, key=lambda s: s.time)
    samples: List[NavSample] = []
    idx = 0
    last_sog = 0.0
    for fix in sorted(fixes, key=lambda f: f.time):
        while idx < len(sogs_sorted) and sogs_sorted[idx].time <= fix.time:
            last_sog = sogs_sorted[idx].sog_ms
            idx += 1
        samples.append(NavSample(fix.time, fix.lat, fix.lon, last_sog))
    return samples


def _classify_runs(
    samples: List[NavSample], speed_threshold_ms: float
) -> List[Tuple[str, List[NavSample]]]:
    labelled = [("stationary" if s.sog_ms < speed_threshold_ms else "moving", s) for s in samples]
    return [(label, [s for _, s in group]) for label, group in groupby(labelled, key=lambda t: t[0])]


def _merge_short_stops(
    runs: List[Tuple[str, List[NavSample]]], min_stop: timedelta
) -> List[Tuple[str, List[NavSample]]]:
    """Stilligperiodes korter dan de drempel tellen niet als havenbezoek en worden bij de reis getrokken."""
    relabelled = []
    for label, group in runs:
        if label == "stationary" and (group[-1].time - group[0].time) < min_stop:
            label = "moving"
        relabelled.append((label, group))

    merged: List[Tuple[str, List[NavSample]]] = []
    for label, group in relabelled:
        if merged and merged[-1][0] == label:
            merged[-1] = (label, merged[-1][1] + group)
        else:
            merged.append((label, group))
    return merged


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
    """Verschil tussen begin- en eindstand van de motor-eigen triptmeter binnen het tijdvak.

    Geeft None terug als deze PGN niet (voldoende) beschikbaar was voor deze reis — bijvoorbeeld
    omdat het apparaat 'm niet verstuurt — in plaats van een misleidende 0.
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


def build_trips(
    fixes: List[PositionFix],
    sogs: List[SogSample],
    engine_samples: List[EngineSample],
    trip_fuel_samples: Optional[List[TripFuelSample]] = None,
    *,
    geocoder: Optional[object] = None,
    speed_threshold_kn: float = 0.5,
    min_stop_minutes: float = 10.0,
) -> List[TripLeg]:
    if geocoder is None:
        geocoder = NoGeocoder()
    if trip_fuel_samples is None:
        trip_fuel_samples = []

    samples = _merge_nav_samples(fixes, sogs)
    if len(samples) < 2:
        return []

    speed_threshold_ms = speed_threshold_kn * _KNOT_IN_MS
    min_stop = timedelta(minutes=min_stop_minutes)

    runs = _classify_runs(samples, speed_threshold_ms)
    runs = _merge_short_stops(runs, min_stop)

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
        depart_place = prev_stay.place if prev_stay else "Onbekend (start buiten logbestand)"
        arrive_place = next_stay.place if next_stay else "Onbekend (einde buiten logbestand)"

        distance_nm = sum(
            _haversine_nm(a.lat, a.lon, b.lat, b.lon) for a, b in zip(group, group[1:])
        )

        trips.append(
            TripLeg(
                depart_time=depart_time,
                arrive_time=arrive_time,
                depart_place=depart_place,
                arrive_place=arrive_place,
                distance_nm=distance_nm,
                fuel_liters=_fuel_liters(engine_samples, depart_time, arrive_time),
                fuel_liters_device=_device_fuel_delta(trip_fuel_samples, depart_time, arrive_time),
                engine_hours=_engine_hours_delta(engine_samples, depart_time, arrive_time),
                track=group,
            )
        )
    return trips
