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
from typing import Dict, FrozenSet, List, Optional, Tuple

from .geocode import Geocoder, NoGeocoder
from .model import DepthSample, EngineSample, PositionFix, SogSample, TripFuelSample

_KNOT_IN_MS = 0.514444
_EARTH_RADIUS_NM = 3440.065
_MAX_INTEGRATION_GAP_H = 1.0  # grotere gaten tussen motorsamples wijzen op een logonderbreking
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
    distance_nm: float
    avg_speed_kn: Optional[float]
    max_speed_kn: Optional[float]
    fuel_liters: float  # berekend door brandstofdebiet (PGN 127489) te integreren over de tijd
    fuel_liters_device: Optional[float]  # motor-eigen triptmeter (PGN 127497), None = niet beschikbaar
    engine_hours: Dict[int, float]  # motor-instance -> gedraaide uren tijdens deze reis
    engine_health: Dict[int, EngineHealth]  # motor-instance -> gezondheidsindicatoren + waarschuwingen
    min_depth_m: Optional[float]  # ondiepste gemeten waterdiepte tijdens deze reis
    min_depth_lat: Optional[float]
    min_depth_lon: Optional[float]
    track: List[NavSample]  # GPS-punten van deze reis, voor bv. GPX-export


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_NM * math.asin(math.sqrt(a))


def _merge_nav_samples(
    fixes: List[PositionFix], sogs: List[SogSample], depths: Optional[List[DepthSample]] = None
) -> List[NavSample]:
    """Combineert positie-, snelheids- en diepte-metingen chronologisch; beide worden forward-filled."""
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


def _split_on_gaps(samples: List[NavSample], max_gap: timedelta) -> List[List[NavSample]]:
    """Splitst de samples op elke plek waar er te lang geen data was.

    Zonder dit zou een reis een groot gat in de data (bv. het apparaat stond een tijd uit, of
    er ontbreken logbestanden) overbruggen als "varend" tot aan het eerstvolgende punt --
    ook al is er in werkelijkheid niets bekend over wat er in dat gat gebeurde. Vooral
    riskant in combinatie met ``_merge_short_stops``: een echt havenbezoek waarvan door het
    gat maar een paar minuten data over is, telt dan niet als stop en de reis "loopt door"
    tot ver na het gat, met een veel te lange gerapporteerde vaartijd tot gevolg (in de
    praktijk gezien: 3:21 vaartijd terwijl de motor maar 0:48 heeft gedraaid)."""
    if not samples:
        return []
    segments: List[List[NavSample]] = [[samples[0]]]
    for previous, current in zip(samples, samples[1:]):
        if current.time - previous.time > max_gap:
            segments.append([])
        segments[-1].append(current)
    return segments


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


def _speed_stats_kn(track: List[NavSample]) -> Tuple[Optional[float], Optional[float]]:
    if not track:
        return None, None
    speeds_kn = [s.sog_ms / _KNOT_IN_MS for s in track]
    return sum(speeds_kn) / len(speeds_kn), max(speeds_kn)


def _min_depth(track: List[NavSample]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Geeft (diepte_m, lat, lon) van het ondiepste gemeten punt, of (None, None, None)."""
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
    """``max_gap_minutes``: hoelang er hooguit geen data mag zijn voordat een reis wordt
    afgekapt (zie ``_split_on_gaps``). Standaard gelijk aan ``min_stop_minutes`` -- eenzelfde
    getal, maar twee verschillende betekenissen: de één is "hoelang moet je stilliggen",
    de ander "hoelang mag er geen data zijn".

    ``min_trip_distance_nm``: reizen die minder dan dit afleggen worden weggefilterd. Vooral
    nodig sinds ``_split_on_gaps``: elke segmentgrens (bestandsgrens of groot gat) kan een paar
    seconden GPS-/snelheidsruis aan de rand bevatten die net boven ``speed_threshold_kn`` komt
    en zo als een nietszeggend "reisje" van een paar meter wordt gezien. Dat is geen echte reis
    (in de praktijk gevonden: 0,0 nm, een paar seconden tot minuten durend, motor uit)."""
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

    trips: List[TripLeg] = []
    for segment in _split_on_gaps(samples, max_gap):
        if len(segment) < 2:
            continue
        trips += _build_trips_for_segment(
            segment,
            engine_samples,
            trip_fuel_samples,
            geocoder=geocoder,
            speed_threshold_ms=speed_threshold_ms,
            min_stop=min_stop,
        )
    return [trip for trip in trips if trip.distance_nm >= min_trip_distance_nm]


def _build_trips_for_segment(
    samples: List[NavSample],
    engine_samples: List[EngineSample],
    trip_fuel_samples: List[TripFuelSample],
    *,
    geocoder: object,
    speed_threshold_ms: float,
    min_stop: timedelta,
) -> List[TripLeg]:
    """Bouwt reizen voor één aaneengesloten stuk data (dus zonder grote gaten erin -- zie
    ``_split_on_gaps``). Een segmentgrens gedraagt zich verder net als een bestandsgrens: een
    reis die tegen zo'n grens aanloopt krijgt "Onbekend (start/einde buiten logbestand)"."""
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
        avg_speed_kn, max_speed_kn = _speed_stats_kn(group)
        min_depth_m, min_depth_lat, min_depth_lon = _min_depth(group)

        trips.append(
            TripLeg(
                depart_time=depart_time,
                arrive_time=arrive_time,
                depart_place=depart_place,
                arrive_place=arrive_place,
                distance_nm=distance_nm,
                avg_speed_kn=avg_speed_kn,
                max_speed_kn=max_speed_kn,
                fuel_liters=_fuel_liters(engine_samples, depart_time, arrive_time),
                fuel_liters_device=_device_fuel_delta(trip_fuel_samples, depart_time, arrive_time),
                engine_hours=_engine_hours_delta(engine_samples, depart_time, arrive_time),
                engine_health=_engine_health(engine_samples, depart_time, arrive_time),
                min_depth_m=min_depth_m,
                min_depth_lat=min_depth_lat,
                min_depth_lon=min_depth_lon,
                track=group,
            )
        )
    return trips
