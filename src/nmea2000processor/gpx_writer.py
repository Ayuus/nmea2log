"""Schrijft reizen weg als één GPX-bestand (track per reis), voor gebruik in navigatiesoftware.

Elke reis wordt een los <trk> met vaartijd, afstand, brandstof en draaiuren in de naam/
beschrijving, zodat je in een kaartprogramma (OpenCPN, Navionics, etc.) in één oogopslag ziet
wat een reis kostte als je erop klikt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional
from xml.etree.ElementTree import Element, ElementTree, SubElement, indent

from .logbook_writer import _format_duration, _format_warnings, _nl_num, _to_local, _trip_utc_offset_hours
from .tripbuilder import TripLeg

_GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"


def _trip_name(trip: TripLeg, utc_offset_hours: Optional[float] = None) -> str:
    # Naam toont lokale tijd (zoals de CSV); <trkpt><time> blijft strikt UTC, zie write_gpx.
    depart_local = _to_local(trip.depart_time, _trip_utc_offset_hours(trip, utc_offset_hours))
    return f"{depart_local:%Y-%m-%d %H:%M} {trip.depart_place} -> {trip.arrive_place}"


def _trip_description(trip: TripLeg) -> str:
    duration = trip.duration
    parts = [
        f"Vaartijd: {_format_duration(duration)}",
        f"Afstand: {_nl_num(trip.distance_nm)} nm",
    ]
    if trip.avg_speed_kn is not None and trip.max_speed_kn is not None:
        parts.append(f"Snelheid: gem. {_nl_num(trip.avg_speed_kn)} kn, max {_nl_num(trip.max_speed_kn)} kn")
    parts.append(f"Brandstof (berekend): {_nl_num(trip.fuel_liters)} L")
    if trip.fuel_liters_device is not None:
        parts.append(f"Brandstof (motorteller): {_nl_num(trip.fuel_liters_device)} L")
    draaiuren = ", ".join(
        f"motor {instance}: {_nl_num(hours)} u" for instance, hours in sorted(trip.engine_hours.items())
    )
    if draaiuren:
        parts.append(f"Draaiuren: {draaiuren}")
    warnings = _format_warnings(trip.engine_health)
    if warnings:
        parts.append(f"Waarschuwingen: {warnings}")
    if trip.min_depth_m is not None:
        parts.append(f"Min. diepte: {_nl_num(trip.min_depth_m)} m")
    return ", ".join(parts)


def write_gpx(trips: Iterable[TripLeg], path: Path, utc_offset_hours: Optional[float] = None) -> None:
    gpx = Element("gpx", version="1.1", creator="nmea2000processor", xmlns=_GPX_NAMESPACE)
    for trip in trips:
        if not trip.track:
            continue
        trk = SubElement(gpx, "trk")
        SubElement(trk, "name").text = _trip_name(trip, utc_offset_hours)
        SubElement(trk, "desc").text = _trip_description(trip)
        trkseg = SubElement(trk, "trkseg")
        for point in trip.track:
            trkpt = SubElement(trkseg, "trkpt", lat=f"{point.lat:.7f}", lon=f"{point.lon:.7f}")
            # NMEA2000-posities zijn GPS-afgeleid en dus in UTC; we bewaren geen tijdzone apart.
            SubElement(trkpt, "time").text = point.time.strftime("%Y-%m-%dT%H:%M:%SZ")

    tree = ElementTree(gpx)
    indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
