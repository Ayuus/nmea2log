"""Schrijft reizen weg als een CSV-logboek (Nederlandse Excel-conventie: ';' als scheidingsteken
en ',' als decimaalteken -- de kolomnamen zelf zijn Engels zodat het bestand ook buiten NL
leesbaar is)."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, Optional

from .tripbuilder import EngineHealth, TripLeg

_FIELDNAMES = [
    "date",
    "departure_time",
    "departure_port",
    "arrival_time",
    "arrival_port",
    "duration",
    "distance_nm",
    "avg_speed_kn",
    "max_speed_kn",
    "fuel_L_calculated",
    "fuel_L_engine_meter",
    "avg_consumption_L_per_hour",
    "avg_consumption_L_per_nm",
    "engine_hours",
    "engine_health",
    "warnings",
    "min_depth_m",
    "min_depth_position",
]


def _nl_num(value: float, decimals: int = 1) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")


def _format_duration(duration: timedelta) -> str:
    total_minutes = int(duration.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}:{minutes:02d}"


def _format_engine_health(engine_health: Dict[int, EngineHealth]) -> str:
    parts = []
    for instance, health in sorted(engine_health.items()):
        bits = []
        if health.oil_pressure_bar_avg is not None:
            bits.append(f"oil {_nl_num(health.oil_pressure_bar_avg)} bar")
        if health.oil_temperature_c_avg is not None:
            bits.append(f"oil temp {_nl_num(health.oil_temperature_c_avg, 0)}°C")
        if health.coolant_temperature_c_avg is not None:
            bits.append(f"coolant {_nl_num(health.coolant_temperature_c_avg, 0)}°C")
        if health.alternator_voltage_v_avg is not None:
            bits.append(f"alternator {_nl_num(health.alternator_voltage_v_avg)} V")
        if health.engine_load_pct_max is not None:
            bits.append(f"load max {_nl_num(health.engine_load_pct_max, 0)}%")
        if bits:
            parts.append(f"engine {instance}: " + ", ".join(bits))
    return "; ".join(parts)


def _format_warnings(engine_health: Dict[int, EngineHealth]) -> str:
    parts = []
    for instance, health in sorted(engine_health.items()):
        if health.warnings:
            parts.append(f"engine {instance}: " + ", ".join(sorted(health.warnings)))
    return "; ".join(parts)


def _estimate_utc_offset_hours(longitude: float) -> int:
    """Ruwe schatting van de tijdzone-offset uit de lengtegraad (15 graden per uur), zonder
    tijdzone-database. Geen zomer-/wintertijd-besef en kan vlak bij een tijdzone-grens tot
    ~1 uur afwijken -- voor een exacte offset kan je die met --utc-offset zelf opleggen."""
    return max(-12, min(14, round(longitude / 15)))


def _trip_utc_offset_hours(trip: TripLeg, fixed_offset: Optional[float]) -> float:
    if fixed_offset is not None:
        return fixed_offset
    if trip.track:
        return _estimate_utc_offset_hours(trip.track[0].lon)
    return 0.0


def _to_local(dt: datetime, offset_hours: float) -> datetime:
    return dt + timedelta(hours=offset_hours)


def _avg_consumption_l_per_nm(trip: TripLeg) -> Optional[float]:
    return trip.fuel_liters / trip.distance_nm if trip.distance_nm > 0 else None


def _engine_hours_text(trip: TripLeg) -> str:
    return ", ".join(
        f"engine {instance}: {_nl_num(hours)} h" for instance, hours in sorted(trip.engine_hours.items())
    )


def _min_depth_position_text(trip: TripLeg) -> str:
    if trip.min_depth_lat is None or trip.min_depth_lon is None:
        return ""
    return f"{trip.min_depth_lat:.4f}, {trip.min_depth_lon:.4f}"


def write_csv(trips: Iterable[TripLeg], path: Path, utc_offset_hours: Optional[float] = None) -> None:
    """``utc_offset_hours``: vaste tijdzone-offset (bv. 2 voor CEST) om op alle reizen toe te
    passen. Standaard (None) wordt de offset per reis geschat uit de vertreklengtegraad."""
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDNAMES, delimiter=";")
        writer.writeheader()
        for trip in trips:
            offset = _trip_utc_offset_hours(trip, utc_offset_hours)
            depart_local = _to_local(trip.depart_time, offset)
            arrive_local = _to_local(trip.arrive_time, offset)

            duration = trip.duration
            duration_h = duration.total_seconds() / 3600.0
            avg_consumption = trip.fuel_liters / duration_h if duration_h > 0 else None
            avg_consumption_per_nm = _avg_consumption_l_per_nm(trip)
            writer.writerow(
                {
                    "date": depart_local.date().isoformat(),
                    "departure_time": depart_local.strftime("%H:%M"),
                    "departure_port": trip.depart_place,
                    "arrival_time": arrive_local.strftime("%H:%M"),
                    "arrival_port": trip.arrive_place,
                    "duration": _format_duration(duration),
                    "distance_nm": _nl_num(trip.distance_nm),
                    "avg_speed_kn": _nl_num(trip.avg_speed_kn) if trip.avg_speed_kn is not None else "",
                    "max_speed_kn": _nl_num(trip.max_speed_kn) if trip.max_speed_kn is not None else "",
                    "fuel_L_calculated": _nl_num(trip.fuel_liters),
                    "fuel_L_engine_meter": _nl_num(trip.fuel_liters_device)
                    if trip.fuel_liters_device is not None
                    else "",
                    "avg_consumption_L_per_hour": _nl_num(avg_consumption) if avg_consumption is not None else "",
                    "avg_consumption_L_per_nm": _nl_num(avg_consumption_per_nm, 2)
                    if avg_consumption_per_nm is not None
                    else "",
                    "engine_hours": _engine_hours_text(trip),
                    "engine_health": _format_engine_health(trip.engine_health),
                    "warnings": _format_warnings(trip.engine_health),
                    "min_depth_m": _nl_num(trip.min_depth_m) if trip.min_depth_m is not None else "",
                    "min_depth_position": _min_depth_position_text(trip),
                }
            )
