"""Writes the whole logbook out as a single, self-contained HTML file: a boat name header,
overall totals (trip count, distance, fuel, engine hours, average consumption), trips grouped
by year and ISO week, and a per-trip route map (Leaflet + OpenStreetMap) that expands inline
when you click a trip's "Map" button.

Everything lives in one file -- there's nothing to keep together or link between. Map tiles and
the Leaflet library load from a CDN when you view the page, so viewing requires internet (the
file itself needs none to generate or to open).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from xml.sax.saxutils import escape

from .logbook_writer import (
    _all_warnings_text,
    _avg_consumption_l_per_nm,
    _engine_hours_text,
    _format_duration,
    _nl_num,
    _to_local,
    _trip_utc_offset_hours,
    _typical_rpm_text,
)
from .tripbuilder import TripLeg

_LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
_LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
_MAX_MAP_POINTS = 500


@dataclass
class _Totals:
    trip_count: int
    distance_nm: float
    fuel_liters: float
    fuel_liters_device: Optional[float]
    moving_hours: float
    engine_hours: Dict[int, float]
    engine_hours_current: Dict[int, float]
    max_speed_kn: Optional[float]


def _compute_totals(trips: List[TripLeg]) -> _Totals:
    distance_nm = sum(trip.distance_nm for trip in trips)
    fuel_liters = sum(trip.fuel_liters for trip in trips)
    device_values = [trip.fuel_liters_device for trip in trips if trip.fuel_liters_device is not None]
    fuel_liters_device = sum(device_values) if device_values else None
    moving_hours = sum(trip.duration.total_seconds() for trip in trips) / 3600.0
    engine_hours: Dict[int, float] = {}
    engine_hours_current: Dict[int, float] = {}
    # ``trips`` is already sorted chronologically by the caller, so the last value seen per
    # engine instance is the most recent known reading of its absolute hour meter.
    for trip in trips:
        for instance, hours in trip.engine_hours.items():
            engine_hours[instance] = engine_hours.get(instance, 0.0) + hours
        for instance, hours in trip.engine_hours_total.items():
            engine_hours_current[instance] = hours

    speeds = [trip.max_speed_kn for trip in trips if trip.max_speed_kn is not None]
    max_speed_kn = max(speeds) if speeds else None

    return _Totals(
        trip_count=len(trips),
        distance_nm=distance_nm,
        fuel_liters=fuel_liters,
        fuel_liters_device=fuel_liters_device,
        moving_hours=moving_hours,
        engine_hours=engine_hours,
        engine_hours_current=engine_hours_current,
        max_speed_kn=max_speed_kn,
    )


def _week_label(iso_year: int, iso_week: int) -> str:
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    sunday = monday + timedelta(days=6)
    return f"Week {iso_week} ({monday:%b %d} - {sunday:%b %d})"


def _decimated_points(trip: TripLeg) -> List[Tuple[float, float]]:
    stride = max(1, len(trip.track) // _MAX_MAP_POINTS)
    decimated = trip.track[::stride]
    if decimated and decimated[-1] is not trip.track[-1]:
        decimated.append(trip.track[-1])
    return [(round(s.lat, 6), round(s.lon, 6)) for s in decimated]


# (background, text) hex pairs for the water-temp badge, cold to warm.
_TEMP_COLORS = (
    (14, "#E6F1FB", "#0C447C"),
    (18, "#E1F5EE", "#085041"),
    (22, "#EAF3DE", "#27500A"),
    (26, "#FAEEDA", "#633806"),
)
_TEMP_COLOR_HOT = ("#FCEBEB", "#791F1F")


def _water_temp_badge_html(trip: TripLeg) -> str:
    if trip.avg_water_temp_c is None:
        return ""
    for threshold, bg, text in _TEMP_COLORS:
        if trip.avg_water_temp_c < threshold:
            break
    else:
        bg, text = _TEMP_COLOR_HOT
    range_text = ""
    if trip.max_water_temp_c - trip.min_water_temp_c > 0.5:
        range_text = (
            f' <span style="opacity:0.75;">({_nl_num(trip.min_water_temp_c)}'
            f"–{_nl_num(trip.max_water_temp_c)}°)</span>"
        )
    return (
        f'<span class="temp-badge" style="background:{bg};color:{text};">'
        f"🌡️ {_nl_num(trip.avg_water_temp_c)}°C{range_text}</span>"
    )


def _totals_html(totals: _Totals) -> str:
    avg_l_per_nm = totals.fuel_liters / totals.distance_nm if totals.distance_nm > 0 else None
    avg_l_per_hour = totals.fuel_liters / totals.moving_hours if totals.moving_hours > 0 else None

    items = [
        ("Trips", str(totals.trip_count)),
        ("Total distance", f"{_nl_num(totals.distance_nm)} nm"),
        ("Total fuel (calculated)", f"{_nl_num(totals.fuel_liters)} L"),
    ]
    if totals.fuel_liters_device is not None:
        items.append(("Total fuel (engine meter)", f"{_nl_num(totals.fuel_liters_device)} L"))
    if avg_l_per_nm is not None:
        items.append(("Avg. consumption", f"{_nl_num(avg_l_per_nm, 2)} L/nm"))
    if avg_l_per_hour is not None:
        items.append(("Avg. consumption", f"{_nl_num(avg_l_per_hour)} L/h"))
    if totals.max_speed_kn is not None:
        items.append(("Top speed", f"{_nl_num(totals.max_speed_kn)} kn"))

    # The engine's own absolute hour meter (for maintenance intervals), as of the most recent
    # trip -- distinct from "hours logged", which only counts time run during this logbook's
    # own trips and misses everything the engine ran before logging started.
    if len(totals.engine_hours_current) == 1:
        hours = next(iter(totals.engine_hours_current.values()))
        items.append(("Engine hour meter", f"{_nl_num(hours)} h"))
    else:
        for instance, hours in sorted(totals.engine_hours_current.items()):
            items.append((f"Engine hour meter, engine {instance}", f"{_nl_num(hours)} h"))

    if len(totals.engine_hours) == 1:
        hours = next(iter(totals.engine_hours.values()))
        items.append(("Hours logged", f"{_nl_num(hours)} h"))
    else:
        for instance, hours in sorted(totals.engine_hours.items()):
            items.append((f"Hours logged, engine {instance}", f"{_nl_num(hours)} h"))

    cards = "".join(
        f'<div class="stat"><div class="stat-label">{escape(label)}</div>'
        f'<div class="stat-value">{escape(value)}</div></div>'
        for label, value in items
    )
    return f'<section class="totals">{cards}</section>'


def _trip_row_html(
    trip: TripLeg,
    idx: int,
    utc_offset_hours: Optional[float],
    trip_uid: Optional[str] = None,
    battery_warning_voltage: Optional[float] = None,
) -> str:
    offset = _trip_utc_offset_hours(trip, utc_offset_hours)
    depart_local = _to_local(trip.depart_time, offset)
    arrive_local = _to_local(trip.arrive_time, offset)
    avg_consumption_nm = _avg_consumption_l_per_nm(trip)

    map_cell = (
        f'<button class="show-map" data-trip="{idx}">Map</button>' if trip.track else ""
    )

    cells = [
        depart_local.strftime("%Y-%m-%d"),
        depart_local.strftime("%H:%M"),
        escape(trip.depart_place),
        arrive_local.strftime("%H:%M"),
        escape(trip.arrive_place),
        _format_duration(trip.duration),
        f"{_nl_num(trip.distance_nm)} nm",
        _nl_num(trip.avg_speed_kn) + " kn" if trip.avg_speed_kn is not None else "",
        _nl_num(trip.max_speed_kn) + " kn" if trip.max_speed_kn is not None else "",
        _nl_num(trip.fuel_liters) + " L",
        f"{_nl_num(avg_consumption_nm, 2)} L/nm" if avg_consumption_nm is not None else "",
        escape(_engine_hours_text(trip)),
        escape(_typical_rpm_text(trip)),
        escape(_all_warnings_text(trip, battery_warning_voltage)),
        _water_temp_badge_html(trip),
        map_cell,
    ]
    row = "".join(f"<td>{cell}</td>" for cell in cells)
    map_row = ""
    if trip.track:
        title = escape(f"{depart_local:%Y-%m-%d %H:%M} {trip.depart_place} -> {trip.arrive_place}")
        map_row = (
            f'<tr class="trip-map-row" data-trip="{idx}" style="display:none">'
            f'<td colspan="16"><div class="trip-map-title">{title}</div>'
            f'<div class="map" id="map-{idx}"></div></td></tr>'
        )
    uid_attr = f' data-uid="{escape(trip_uid)}"' if trip_uid else ""
    return f'<tr class="trip-row"{uid_attr}>{row}</tr>{map_row}'


_HEADERS = [
    "Date",
    "Dep.",
    "From",
    "Arr.",
    "To",
    "Duration",
    "Distance",
    "Avg speed",
    "Max speed",
    "Fuel",
    "L/nm",
    "Engine hours",
    "RPM",
    "Warnings",
    "Water temp",
    "Route",
]


def write_html_logbook(
    trips: Iterable[TripLeg],
    path: Path,
    boat_name: Optional[str] = None,
    utc_offset_hours: Optional[float] = None,
    trip_uids: Optional[List[str]] = None,
    battery_warning_voltage: Optional[float] = None,
) -> None:
    """``trip_uids``: one id per trip, in the same order as ``trips`` *before* sorting -- e.g.
    from ``trip_ids.assign_trip_ids(trips)``. Embedded as an invisible ``data-uid`` attribute on
    each trip row so a future feature could key off it instead of a timestamp that could shift
    with a trip-recognition fix. Not used by anything else yet."""
    trips = list(trips)
    uid_by_trip = {id(trip): uid for trip, uid in zip(trips, trip_uids)} if trip_uids is not None else {}
    trips = sorted(trips, key=lambda t: t.depart_time)

    by_week: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for idx, trip in enumerate(trips):
        offset = _trip_utc_offset_hours(trip, utc_offset_hours)
        local_date = _to_local(trip.depart_time, offset).date()
        iso_year, iso_week, _ = local_date.isocalendar()
        by_week[(iso_year, iso_week)].append(idx)

    header_html = "".join(f"<th>{escape(h)}</th>" for h in _HEADERS)

    sections: List[str] = []
    for iso_year in sorted({y for y, _ in by_week}, reverse=True):
        weeks_in_year = sorted((w for y, w in by_week if y == iso_year), reverse=True)
        year_trips = [trips[i] for w in weeks_in_year for i in by_week[(iso_year, w)]]
        week_sections = []
        for iso_week in weeks_in_year:
            indices = by_week[(iso_year, iso_week)]
            rows_html = "".join(
                _trip_row_html(
                    trips[i], i, utc_offset_hours, uid_by_trip.get(id(trips[i])), battery_warning_voltage
                )
                for i in indices
            )
            week_sections.append(
                f'<section class="week"><h3>{escape(_week_label(iso_year, iso_week))}</h3>'
                f'<table class="trips"><thead><tr>{header_html}</tr></thead>'
                f"<tbody>{rows_html}</tbody></table></section>"
            )
        sections.append(
            f'<section class="year"><h2>{iso_year}</h2>'
            f"{_totals_html(_compute_totals(year_trips))}"
            f'{"".join(week_sections)}</section>'
        )

    trip_data = {
        idx: {"points": _decimated_points(trip)}
        for idx, trip in enumerate(trips)
        if trip.track
    }
    trips_json = json.dumps(trip_data).replace("</", "<\\/")

    title = f"{boat_name} - Sailing Logbook" if boat_name else "Sailing Logbook"
    heading = f"{escape(boat_name)} &mdash; Sailing Logbook" if boat_name else "Sailing Logbook"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<link rel="stylesheet" href="{_LEAFLET_CSS}">
<script src="{_LEAFLET_JS}"></script>
<style>
  body {{ font-family: sans-serif; margin: 0; padding: 1.5em; background: #f7f7f8; color: #1a1a1a; }}
  h1 {{ margin-bottom: 0.2em; }}
  h2 {{ margin-top: 2em; border-bottom: 2px solid #1a6ecc; padding-bottom: 0.2em; }}
  h3 {{ margin-top: 1.5em; color: #333; }}
  .totals {{ display: flex; flex-wrap: wrap; gap: 1em; margin: 1em 0 2em; }}
  .stat {{ background: white; border-radius: 8px; padding: 0.8em 1.2em; box-shadow: 0 1px 3px rgba(0,0,0,0.1); min-width: 140px; }}
  .stat-label {{ font-size: 0.8em; color: #666; }}
  .stat-value {{ font-size: 1.3em; font-weight: 600; }}
  table.trips {{ border-collapse: collapse; width: 100%; background: white; margin-bottom: 1em; }}
  table.trips th, table.trips td {{ padding: 0.4em 0.6em; border-bottom: 1px solid #eee; text-align: left; font-size: 0.9em; }}
  table.trips th {{ background: #f0f0f0; }}
  .show-map {{ cursor: pointer; border: 1px solid #1a6ecc; background: white; color: #1a6ecc; border-radius: 4px; padding: 0.2em 0.6em; }}
  .show-map:hover {{ background: #1a6ecc; color: white; }}
  .trip-map-title {{ font-weight: 600; margin-bottom: 0.4em; }}
  .temp-badge {{ display: inline-block; border-radius: 12px; padding: 0.15em 0.6em; font-size: 0.85em; white-space: nowrap; }}
  .map {{ height: 350px; }}
</style>
</head>
<body>
<h1>{heading}</h1>
{_totals_html(_compute_totals(trips))}
{"".join(sections)}
<script>
const TRIPS = {trips_json};
document.querySelectorAll('.show-map').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    var idx = btn.dataset.trip;
    var row = document.querySelector('.trip-map-row[data-trip="' + idx + '"]');
    var visible = row.style.display !== 'none';
    row.style.display = visible ? 'none' : '';
    btn.textContent = visible ? 'Map' : 'Hide map';
    if (!visible && !row.dataset.initialized) {{
      row.dataset.initialized = '1';
      var map = L.map('map-' + idx);
      L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        maxZoom: 19,
        attribution: '&copy; OpenStreetMap contributors'
      }}).addTo(map);
      var points = TRIPS[idx].points;
      var line = L.polyline(points, {{color: '#1a6ecc', weight: 3}}).addTo(map);
      L.marker(points[0]).addTo(map).bindPopup('Departure');
      L.marker(points[points.length - 1]).addTo(map).bindPopup('Arrival');
      map.fitBounds(line.getBounds(), {{padding: [20, 20]}});
      setTimeout(function() {{ map.invalidateSize(); }}, 0);
    }}
  }});
}});
</script>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")
