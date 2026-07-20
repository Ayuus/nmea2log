"""Schrijft reizen weg als één GPX-bestand (track per reis), voor gebruik in navigatiesoftware.

Elke reis wordt een los <trk> met vaartijd, afstand, brandstof en draaiuren in de naam/
beschrijving, zodat je in een kaartprogramma (OpenCPN, Navionics, etc.) in één oogopslag ziet
wat een reis kostte als je erop klikt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable
from xml.etree.ElementTree import Element, ElementTree, SubElement, indent

from .logbook_writer import _format_duration, _nl_num
from .tripbuilder import TripLeg

_GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"


def _trip_name(trip: TripLeg) -> str:
    return f"{trip.depart_time:%Y-%m-%d %H:%M} {trip.depart_place} -> {trip.arrive_place}"


def _trip_description(trip: TripLeg) -> str:
    duration = trip.arrive_time - trip.depart_time
    parts = [
        f"Vaartijd: {_format_duration(duration)}",
        f"Afstand: {_nl_num(trip.distance_nm)} nm",
        f"Brandstof: {_nl_num(trip.fuel_liters)} L",
    ]
    draaiuren = ", ".join(
        f"motor {instance}: {_nl_num(hours)} u" for instance, hours in sorted(trip.engine_hours.items())
    )
    if draaiuren:
        parts.append(f"Draaiuren: {draaiuren}")
    return ", ".join(parts)


def write_gpx(trips: Iterable[TripLeg], path: Path) -> None:
    gpx = Element("gpx", version="1.1", creator="nmea2000processor", xmlns=_GPX_NAMESPACE)
    for trip in trips:
        if not trip.track:
            continue
        trk = SubElement(gpx, "trk")
        SubElement(trk, "name").text = _trip_name(trip)
        SubElement(trk, "desc").text = _trip_description(trip)
        trkseg = SubElement(trk, "trkseg")
        for point in trip.track:
            trkpt = SubElement(trkseg, "trkpt", lat=f"{point.lat:.7f}", lon=f"{point.lon:.7f}")
            # NMEA2000-posities zijn GPS-afgeleid en dus in UTC; we bewaren geen tijdzone apart.
            SubElement(trkpt, "time").text = point.time.strftime("%Y-%m-%dT%H:%M:%SZ")

    tree = ElementTree(gpx)
    indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
