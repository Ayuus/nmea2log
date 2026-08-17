"""Writes trips out as a CSV logbook (Dutch Excel convention: ';' as the delimiter and ',' as
the decimal separator -- the column names themselves are English so the file is also readable
outside the Netherlands)."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, Optional

from .tripbuilder import BatteryHealth, EngineHealth, TripLeg

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
    "typical_rpm",
    "engine_health",
    "warnings",
    "min_depth_m",
    "min_depth_position",
    "avg_water_temp_c",
    "min_water_temp_c",
    "max_water_temp_c",
    "roll_variation_deg",
    "pitch_variation_deg",
    "roll_range_deg",
    "pitch_range_deg",
]


def _nl_num(value: float, decimals: int = 1) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")


def _duration_minutes(duration: timedelta) -> int:
    """Rounded to the nearest minute -- shared with html_writer's totals computation so the
    displayed "Totale uren" always exactly equals the sum of the individual "Duur" column values
    a user would get by adding them up by hand (found in practice: floor-rounding each trip's own
    display value while summing the *unrounded* durations for "Totale uren" made the two
    disagree by several minutes)."""
    return round(duration.total_seconds() / 60)


def _format_duration(duration: timedelta) -> str:
    hours, minutes = divmod(_duration_minutes(duration), 60)
    return f"{hours}:{minutes:02d}"


def _format_engine_health(engine_health: Dict[int, EngineHealth]) -> str:
    # Only label per engine instance when there's more than one -- with a single engine the
    # "engine 0:" prefix is just noise repeated on every row.
    single_engine = len(engine_health) == 1
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
            prefix = "" if single_engine else f"engine {instance}: "
            parts.append(prefix + ", ".join(bits))
    return "; ".join(parts)


def _format_warnings(engine_health: Dict[int, EngineHealth]) -> str:
    single_engine = len(engine_health) == 1
    parts = []
    for instance, health in sorted(engine_health.items()):
        if health.warnings:
            prefix = "" if single_engine else f"engine {instance}: "
            parts.append(prefix + ", ".join(sorted(health.warnings)))
    return "; ".join(parts)


def _battery_warning_text(battery_health: Dict[int, BatteryHealth], threshold: Optional[float]) -> str:
    """Flags a battery instance whose voltage dropped below ``threshold`` at any point during
    the trip. Unlike engine warnings (manufacturer-defined bit flags), this is a numeric
    threshold we define ourselves -- see --battery-warning-voltage."""
    if threshold is None:
        return ""
    single_battery = len(battery_health) == 1
    parts = []
    for instance, health in sorted(battery_health.items()):
        if health.min_voltage_v is not None and health.min_voltage_v < threshold:
            prefix = "" if single_battery else f"battery {instance}: "
            parts.append(f"{prefix}low battery {_nl_num(health.min_voltage_v)} V")
    return "; ".join(parts)


def _all_warnings_text(trip: TripLeg, battery_warning_voltage: Optional[float] = None) -> str:
    parts = [
        text
        for text in (
            _format_warnings(trip.engine_health),
            _battery_warning_text(trip.battery_health, battery_warning_voltage),
        )
        if text
    ]
    return "; ".join(parts)


def _last_sunday(year: int, month: int) -> date:
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = next_month - timedelta(days=1)
    return last_day - timedelta(days=(last_day.weekday() - 6) % 7)


def _is_eu_dst(when_utc: datetime) -> bool:
    """EU summer time runs from 01:00 UTC on the last Sunday of March to 01:00 UTC on the last
    Sunday of October, every year, with no exceptions -- unlike most of the rest of the world,
    this doesn't need a timezone database to compute correctly."""
    year = when_utc.year
    dst_start = datetime.combine(_last_sunday(year, 3), datetime.min.time().replace(hour=1))
    dst_end = datetime.combine(_last_sunday(year, 10), datetime.min.time().replace(hour=1))
    return dst_start <= when_utc < dst_end


# Rough bounding boxes (lat, lon) for Western Europe's two civil timezones. Longitude alone
# can't tell CET France apart from WET Portugal/UK/Ireland -- they overlap in longitude despite
# using different zones for political/historical reasons, not solar ones -- but combined with
# latitude these boxes are good enough to tell them apart without a timezone database.
_WET_BOXES = (
    # UK & Ireland
    (49.5, 61.0, -11.0, 2.0),
    # Portugal (mainland)
    (36.8, 42.2, -9.6, -6.0),
)
# France, Benelux, Germany, Switzerland/Austria, Denmark, Italy, Spain -- deliberately not
# extended further east (Poland, the Balkans, ...) where the solar estimate and the real CET/EET
# border both roughly agree anyway, so there's little to gain and more risk of guessing wrong.
_CET_BOX = (36.0, 71.0, -9.5, 15.5)


def _base_offset_hours(lat: float, longitude: float) -> Optional[int]:
    """Base (winter) UTC offset for the Western European civil timezones, or None outside that
    region -- callers fall back to a plain solar-longitude estimate in that case."""
    for lat_min, lat_max, lon_min, lon_max in _WET_BOXES:
        if lat_min <= lat <= lat_max and lon_min <= longitude <= lon_max:
            return 0  # WET/WEST: UTC+0 winter, UTC+1 summer
    lat_min, lat_max, lon_min, lon_max = _CET_BOX
    if lat_min <= lat <= lat_max and lon_min <= longitude <= lon_max:
        return 1  # CET/CEST: UTC+1 winter, UTC+2 summer
    return None


def _estimate_utc_offset_hours(lat: float, longitude: float, when_utc: datetime) -> int:
    """Estimates the timezone offset without a timezone database. Within Western Europe, uses
    the actual CET/CEST vs. WET/WEST civil zones (see ``_base_offset_hours``) rather than pure
    solar longitude -- France, for instance, is geographically in the same longitude band as the
    UK but observes Central European Time, a full hour off from what longitude alone would
    suggest. Outside that region, falls back to a plain solar estimate (15 degrees per hour),
    which is at best a rough approximation of the real, politically-defined timezone -- use
    --utc-offset to force an exact offset yourself if that matters to you there. Even within
    Europe this is still just an estimate and can be off by up to ~1 hour near a timezone
    border."""
    base = _base_offset_hours(lat, longitude)
    if base is None:
        base = max(-12, min(14, round(longitude / 15)))
    if _is_eu_dst(when_utc):
        base += 1
    return base


def _trip_utc_offset_hours(trip: TripLeg, fixed_offset: Optional[float]) -> float:
    if fixed_offset is not None:
        return fixed_offset
    if trip.track:
        return _estimate_utc_offset_hours(trip.track[0].lat, trip.track[0].lon, trip.depart_time)
    return 0.0


def _to_local(dt: datetime, offset_hours: float) -> datetime:
    return dt + timedelta(hours=offset_hours)


def _avg_consumption_l_per_nm(trip: TripLeg) -> Optional[float]:
    return trip.fuel_liters / trip.distance_nm if trip.distance_nm > 0 else None


def _engine_hours_text(trip: TripLeg) -> str:
    if len(trip.engine_hours) == 1:
        hours = next(iter(trip.engine_hours.values()))
        return f"{_nl_num(hours)} h"
    return ", ".join(
        f"engine {instance}: {_nl_num(hours)} h" for instance, hours in sorted(trip.engine_hours.items())
    )


def _typical_rpm_text(trip: TripLeg) -> str:
    if not trip.typical_rpm:
        return ""
    if len(trip.typical_rpm) == 1:
        return f"{next(iter(trip.typical_rpm.values())):.0f}"
    return ", ".join(
        f"engine {instance}: {rpm:.0f}" for instance, rpm in sorted(trip.typical_rpm.items())
    )


def _min_depth_position_text(trip: TripLeg) -> str:
    if trip.min_depth_lat is None or trip.min_depth_lon is None:
        return ""
    return f"{trip.min_depth_lat:.4f}, {trip.min_depth_lon:.4f}"


def write_csv(
    trips: Iterable[TripLeg],
    path: Path,
    utc_offset_hours: Optional[float] = None,
    battery_warning_voltage: Optional[float] = None,
) -> None:
    """``utc_offset_hours``: fixed timezone offset (e.g. 2 for CEST) to apply to all trips.
    Default (None) estimates the offset per trip from the departure longitude.

    ``battery_warning_voltage``: flags a trip's "warnings" column if the battery voltage
    dropped below this at any point (see --battery-warning-voltage)."""
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
                    "typical_rpm": _typical_rpm_text(trip),
                    "engine_health": _format_engine_health(trip.engine_health),
                    "warnings": _all_warnings_text(trip, battery_warning_voltage),
                    "min_depth_m": _nl_num(trip.min_depth_m) if trip.min_depth_m is not None else "",
                    "min_depth_position": _min_depth_position_text(trip),
                    "avg_water_temp_c": _nl_num(trip.avg_water_temp_c) if trip.avg_water_temp_c is not None else "",
                    "min_water_temp_c": _nl_num(trip.min_water_temp_c) if trip.min_water_temp_c is not None else "",
                    "max_water_temp_c": _nl_num(trip.max_water_temp_c) if trip.max_water_temp_c is not None else "",
                    "roll_variation_deg": _nl_num(trip.roll_variation_deg)
                    if trip.roll_variation_deg is not None
                    else "",
                    "pitch_variation_deg": _nl_num(trip.pitch_variation_deg)
                    if trip.pitch_variation_deg is not None
                    else "",
                    "roll_range_deg": _nl_num(trip.roll_range_deg) if trip.roll_range_deg is not None else "",
                    "pitch_range_deg": _nl_num(trip.pitch_range_deg) if trip.pitch_range_deg is not None else "",
                }
            )
