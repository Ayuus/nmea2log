"""Schrijft reizen weg als een CSV-logboek (Nederlandse Excel-conventie: ';' als scheidingsteken)."""

from __future__ import annotations

import csv
from datetime import timedelta
from pathlib import Path
from typing import Dict, Iterable

from .tripbuilder import EngineHealth, TripLeg

_FIELDNAMES = [
    "datum",
    "vertrektijd",
    "vertrekhaven",
    "aankomsttijd",
    "aankomsthaven",
    "vaartijd",
    "afstand_nm",
    "gem_snelheid_kn",
    "max_snelheid_kn",
    "brandstof_L_berekend",
    "brandstof_L_motorteller",
    "gem_verbruik_L_per_uur",
    "draaiuren",
    "motorgezondheid",
    "waarschuwingen",
    "min_diepte_m",
    "min_diepte_positie",
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
            bits.append(f"olie {_nl_num(health.oil_pressure_bar_avg)} bar")
        if health.oil_temperature_c_avg is not None:
            bits.append(f"olietemp {_nl_num(health.oil_temperature_c_avg, 0)}°C")
        if health.coolant_temperature_c_avg is not None:
            bits.append(f"koelvloeistof {_nl_num(health.coolant_temperature_c_avg, 0)}°C")
        if health.alternator_voltage_v_avg is not None:
            bits.append(f"alternator {_nl_num(health.alternator_voltage_v_avg)} V")
        if health.engine_load_pct_max is not None:
            bits.append(f"belasting max {_nl_num(health.engine_load_pct_max, 0)}%")
        if bits:
            parts.append(f"motor {instance}: " + ", ".join(bits))
    return "; ".join(parts)


def _format_warnings(engine_health: Dict[int, EngineHealth]) -> str:
    parts = []
    for instance, health in sorted(engine_health.items()):
        if health.warnings:
            parts.append(f"motor {instance}: " + ", ".join(sorted(health.warnings)))
    return "; ".join(parts)


def write_csv(trips: Iterable[TripLeg], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDNAMES, delimiter=";")
        writer.writeheader()
        for trip in trips:
            duration = trip.arrive_time - trip.depart_time
            duration_h = duration.total_seconds() / 3600.0
            avg_consumption = trip.fuel_liters / duration_h if duration_h > 0 else None
            draaiuren = ", ".join(
                f"motor {instance}: {_nl_num(hours)} u" for instance, hours in sorted(trip.engine_hours.items())
            )
            min_diepte_positie = (
                f"{trip.min_depth_lat:.4f}, {trip.min_depth_lon:.4f}"
                if trip.min_depth_lat is not None and trip.min_depth_lon is not None
                else ""
            )
            writer.writerow(
                {
                    "datum": trip.depart_time.date().isoformat(),
                    "vertrektijd": trip.depart_time.strftime("%H:%M"),
                    "vertrekhaven": trip.depart_place,
                    "aankomsttijd": trip.arrive_time.strftime("%H:%M"),
                    "aankomsthaven": trip.arrive_place,
                    "vaartijd": _format_duration(duration),
                    "afstand_nm": _nl_num(trip.distance_nm),
                    "gem_snelheid_kn": _nl_num(trip.avg_speed_kn) if trip.avg_speed_kn is not None else "",
                    "max_snelheid_kn": _nl_num(trip.max_speed_kn) if trip.max_speed_kn is not None else "",
                    "brandstof_L_berekend": _nl_num(trip.fuel_liters),
                    "brandstof_L_motorteller": _nl_num(trip.fuel_liters_device)
                    if trip.fuel_liters_device is not None
                    else "",
                    "gem_verbruik_L_per_uur": _nl_num(avg_consumption) if avg_consumption is not None else "",
                    "draaiuren": draaiuren,
                    "motorgezondheid": _format_engine_health(trip.engine_health),
                    "waarschuwingen": _format_warnings(trip.engine_health),
                    "min_diepte_m": _nl_num(trip.min_depth_m) if trip.min_depth_m is not None else "",
                    "min_diepte_positie": min_diepte_positie,
                }
            )
