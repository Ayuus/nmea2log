"""Writes trips out as a single GPX file (one track per trip), for use in navigation software.

Each trip becomes its own <trk> with duration, distance, fuel, and engine hours in the
name/description, so a map program (OpenCPN, Navionics, etc.) shows at a glance what a trip
cost when you click on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional
from xml.etree.ElementTree import Element, ElementTree, SubElement, indent

from .logbook_writer import (
    _all_warnings_text,
    _engine_hours_text,
    _format_duration,
    _nl_num,
    _to_local,
    _trip_utc_offset_hours,
    _typical_rpm_text,
)
from .tripbuilder import TripLeg

_GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"


def _trip_name(trip: TripLeg, utc_offset_hours: Optional[float] = None) -> str:
    # Name shows local time (like the CSV); <trkpt><time> stays strictly UTC, see write_gpx.
    depart_local = _to_local(trip.depart_time, _trip_utc_offset_hours(trip, utc_offset_hours))
    return f"{depart_local:%Y-%m-%d %H:%M} {trip.depart_place} -> {trip.arrive_place}"


def _trip_description(trip: TripLeg, battery_warning_voltage: Optional[float] = None) -> str:
    duration = trip.duration
    parts = [
        f"Duration: {_format_duration(duration)}",
        f"Distance: {_nl_num(trip.distance_nm)} nm",
    ]
    if trip.avg_speed_kn is not None and trip.max_speed_kn is not None:
        parts.append(f"Speed: avg {_nl_num(trip.avg_speed_kn)} kn, max {_nl_num(trip.max_speed_kn)} kn")
    parts.append(f"Fuel (calculated): {_nl_num(trip.fuel_liters)} L")
    if trip.fuel_liters_device is not None:
        parts.append(f"Fuel (engine meter): {_nl_num(trip.fuel_liters_device)} L")
    engine_hours = _engine_hours_text(trip)
    if engine_hours:
        parts.append(f"Engine hours: {engine_hours}")
    typical_rpm = _typical_rpm_text(trip)
    if typical_rpm:
        parts.append(f"Typical RPM: {typical_rpm}")
    warnings = _all_warnings_text(trip, battery_warning_voltage)
    if warnings:
        parts.append(f"Warnings: {warnings}")
    if trip.min_depth_m is not None:
        parts.append(f"Min. depth: {_nl_num(trip.min_depth_m)} m")
    if trip.avg_water_temp_c is not None:
        range_text = (
            f" ({_nl_num(trip.min_water_temp_c)}-{_nl_num(trip.max_water_temp_c)})"
            if trip.max_water_temp_c - trip.min_water_temp_c > 0.5
            else ""
        )
        parts.append(f"Water temp: {_nl_num(trip.avg_water_temp_c)}°C{range_text}")
    if trip.roll_variation_deg is not None:
        peak_text = f", peak {_nl_num(trip.roll_range_deg)}°" if trip.roll_range_deg is not None else ""
        parts.append(f"Roll variation: ±{_nl_num(trip.roll_variation_deg)}°{peak_text}")
    if trip.pitch_variation_deg is not None:
        peak_text = f", peak {_nl_num(trip.pitch_range_deg)}°" if trip.pitch_range_deg is not None else ""
        parts.append(f"Pitch variation: ±{_nl_num(trip.pitch_variation_deg)}°{peak_text}")
    return ", ".join(parts)


def write_gpx(
    trips: Iterable[TripLeg],
    path: Path,
    utc_offset_hours: Optional[float] = None,
    battery_warning_voltage: Optional[float] = None,
) -> None:
    gpx = Element("gpx", version="1.1", creator="nmea2log", xmlns=_GPX_NAMESPACE)
    for trip in trips:
        if not trip.track:
            continue
        trk = SubElement(gpx, "trk")
        SubElement(trk, "name").text = _trip_name(trip, utc_offset_hours)
        SubElement(trk, "desc").text = _trip_description(trip, battery_warning_voltage)
        trkseg = SubElement(trk, "trkseg")
        for point in trip.track:
            trkpt = SubElement(trkseg, "trkpt", lat=f"{point.lat:.7f}", lon=f"{point.lon:.7f}")
            # NMEA2000 positions are GPS-derived and thus UTC; we don't store a separate timezone.
            SubElement(trkpt, "time").text = point.time.strftime("%Y-%m-%dT%H:%M:%SZ")

    tree = ElementTree(gpx)
    indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
