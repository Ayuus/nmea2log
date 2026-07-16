"""Schrijft reizen weg als een CSV-logboek (Nederlandse Excel-conventie: ';' als scheidingsteken)."""

from __future__ import annotations

import csv
from datetime import timedelta
from pathlib import Path
from typing import Iterable

from .tripbuilder import TripLeg

_FIELDNAMES = [
    "datum",
    "vertrektijd",
    "vertrekhaven",
    "aankomsttijd",
    "aankomsthaven",
    "vaartijd",
    "afstand_nm",
    "brandstof_L",
    "gem_verbruik_L_per_uur",
    "draaiuren",
]


def _nl_num(value: float, decimals: int = 1) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")


def _format_duration(duration: timedelta) -> str:
    total_minutes = int(duration.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}:{minutes:02d}"


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
            writer.writerow(
                {
                    "datum": trip.depart_time.date().isoformat(),
                    "vertrektijd": trip.depart_time.strftime("%H:%M"),
                    "vertrekhaven": trip.depart_place,
                    "aankomsttijd": trip.arrive_time.strftime("%H:%M"),
                    "aankomsthaven": trip.arrive_place,
                    "vaartijd": _format_duration(duration),
                    "afstand_nm": _nl_num(trip.distance_nm),
                    "brandstof_L": _nl_num(trip.fuel_liters),
                    "gem_verbruik_L_per_uur": _nl_num(avg_consumption) if avg_consumption is not None else "",
                    "draaiuren": draaiuren,
                }
            )
