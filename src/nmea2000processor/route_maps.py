"""Genereert één HTML-bestand met per reis een kaart van de vaarroute (Leaflet + OpenStreetMap),
zodat je vanuit de 'route'-kolom in het ODS-logboek (zie ``ods_writer.py``) kunt doorklikken naar
de route op een kaart. Kaarttegels en de Leaflet-bibliotheek komen van een CDN, dus bekijken
vereist internet -- net als de havennaam-opzoeking is dat optioneel/best-effort en geen
dependency aan de Python-kant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional
from xml.sax.saxutils import escape

from .logbook_writer import _to_local, _trip_utc_offset_hours
from .tripbuilder import TripLeg

_LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
_LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"


def trip_anchor(index: int) -> str:
    """Fragment-id voor reis ``index`` (0-based) -- gedeeld tussen deze module en ``ods_writer``
    zodat de hyperlinks in het ODS-bestand precies bij de juiste kaart uitkomen."""
    return f"trip-{index}"


def write_route_maps(
    trips: Iterable[TripLeg], path: Path, utc_offset_hours: Optional[float] = None
) -> None:
    sections: List[str] = []
    scripts: List[str] = []
    for idx, trip in enumerate(trips):
        if not trip.track:
            continue
        offset = _trip_utc_offset_hours(trip, utc_offset_hours)
        depart_local = _to_local(trip.depart_time, offset)
        title = f"{depart_local:%Y-%m-%d %H:%M} {trip.depart_place} → {trip.arrive_place}"
        anchor = trip_anchor(idx)
        sections.append(
            f'<section id="{anchor}">\n'
            f"  <h2>{escape(title)}</h2>\n"
            f'  <div class="map" id="map-{idx}"></div>\n'
            f"</section>"
        )
        coords = ", ".join(f"[{s.lat:.6f},{s.lon:.6f}]" for s in trip.track)
        scripts.append(
            f"""(function() {{
  var map = L.map('map-{idx}');
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }}).addTo(map);
  var points = [{coords}];
  var line = L.polyline(points, {{color: '#1a6ecc', weight: 3}}).addTo(map);
  L.marker(points[0]).addTo(map).bindPopup('Departure');
  L.marker(points[points.length - 1]).addTo(map).bindPopup('Arrival');
  map.fitBounds(line.getBounds(), {{padding: [20, 20]}});
}})();"""
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sailing routes</title>
<link rel="stylesheet" href="{_LEAFLET_CSS}">
<script src="{_LEAFLET_JS}"></script>
<style>
  body {{ font-family: sans-serif; margin: 0; padding: 1em; }}
  .map {{ height: 400px; margin-bottom: 2em; }}
  h2 {{ margin-bottom: 0.3em; }}
</style>
</head>
<body>
<h1>Sailing routes</h1>
{"".join(sections)}
<script>
{"".join(scripts)}
</script>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")
