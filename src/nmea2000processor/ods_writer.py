"""Schrijft een OpenDocument Spreadsheet (.ods) -- de open (ISO/IEC 26300) tegenhanger van een
Excel-werkmap, opent net zo goed in Excel als in LibreOffice/OpenOffice Calc. Twee bladen:
'Logbook' (per reis, incl. een 'route'-kolom met een klein kaartplaatje + routelijn die
doorklikt naar de volledige interactieve kaart uit ``route_maps.py``) en 'Totals' (opgeteld
verbruik/draaiuren/afstand over alle reizen).

Een .ods is een zip-archief met XML erin; we bouwen 'm met alleen de standaardbibliotheek
(``zipfile`` + ``xml.etree``), zodat de app dependency-vrij blijft. Het kaartplaatje zelf is een
losse OpenStreetMap-tegel (zie ``static_map.py``) die ongewijzigd wordt ingebed -- de routelijn
erover is een aparte, door ODF zelf getekende vectorlijn (geen pixelbewerking van onze kant
nodig)."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from xml.etree.ElementTree import Element, SubElement, register_namespace, tostring

from .logbook_writer import (
    _avg_consumption_l_per_nm,
    _engine_hours_text,
    _format_duration,
    _format_engine_health,
    _format_warnings,
    _nl_num,
    _to_local,
    _trip_utc_offset_hours,
)
from .route_maps import trip_anchor
from .static_map import DEFAULT_CACHE_DIR, TILE_SIZE, build_route_thumbnail
from .tripbuilder import TripLeg

_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
}

_MIMETYPE = "application/vnd.oasis.opendocument.spreadsheet"

for _prefix, _uri in _NS.items():
    register_namespace(_prefix, _uri)

_HEADERS = [
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
    "route",
]

_THUMBNAIL_SIZE_CM = 3.0
_ROUTE_COLUMN_WIDTH_CM = 3.4
_DEFAULT_COLUMN_WIDTH_CM = 2.2
_ROW_HEIGHT_CM = 3.2
_ROUTE_LINE_COLOR = "#1a6ecc"


def _q(prefix: str, tag: str) -> str:
    return f"{{{_NS[prefix]}}}{tag}"


def _row() -> Element:
    return Element(_q("table", "table-row"))


def _str_cell(row: Element, text: str) -> None:
    cell = SubElement(row, _q("table", "table-cell"), {_q("office", "value-type"): "string"})
    p = SubElement(cell, _q("text", "p"))
    p.text = text


def _num_cell(row: Element, value: Optional[float], decimals: int = 1) -> None:
    if value is None:
        SubElement(row, _q("table", "table-cell"))
        return
    cell = SubElement(
        row,
        _q("table", "table-cell"),
        {_q("office", "value-type"): "float", _q("office", "value"): repr(float(value))},
    )
    p = SubElement(cell, _q("text", "p"))
    p.text = _nl_num(value, decimals)


def _map_cell(row: Element, href: Optional[str], picture_name: Optional[str], points_px) -> None:
    """Cel met een klein kaartplaatje + routelijn erover, ingebed als OpenStreetMap-tegel +
    ODF-vectorlijn (zie modulebeschrijving). Zonder plaatje/href valt dit terug op een gewone
    tekstcel (bv. als de tegel niet kon worden opgehaald of er geen track is)."""
    cell = SubElement(row, _q("table", "table-cell"), {_q("office", "value-type"): "string"})
    if not picture_name or not points_px:
        p = SubElement(cell, _q("text", "p"))
        if href:
            a = SubElement(p, _q("text", "a"), {_q("xlink", "type"): "simple", _q("xlink", "href"): href})
            a.text = "route"
        return
    p = SubElement(cell, _q("text", "p"))
    parent: Element = p
    if href:
        parent = SubElement(p, _q("text", "a"), {_q("xlink", "type"): "simple", _q("xlink", "href"): href})

    frame = SubElement(
        parent,
        _q("draw", "frame"),
        {
            _q("svg", "width"): f"{_THUMBNAIL_SIZE_CM}cm",
            _q("svg", "height"): f"{_THUMBNAIL_SIZE_CM}cm",
            _q("svg", "x"): "0cm",
            _q("svg", "y"): "0cm",
            _q("text", "anchor-type"): "paragraph",
            _q("draw", "z-index"): "0",
        },
    )
    SubElement(
        frame,
        _q("draw", "image"),
        {
            _q("xlink", "href"): f"Pictures/{picture_name}",
            _q("xlink", "type"): "simple",
            _q("xlink", "show"): "embed",
            _q("xlink", "actuate"): "onLoad",
        },
    )

    points_text = " ".join(f"{round(x)},{round(y)}" for x, y in points_px)
    SubElement(
        parent,
        _q("draw", "polyline"),
        {
            _q("svg", "x"): "0cm",
            _q("svg", "y"): "0cm",
            _q("svg", "width"): f"{_THUMBNAIL_SIZE_CM}cm",
            _q("svg", "height"): f"{_THUMBNAIL_SIZE_CM}cm",
            _q("svg", "viewBox"): f"0 0 {TILE_SIZE} {TILE_SIZE}",
            _q("draw", "points"): points_text,
            _q("draw", "style-name"): "grLine",
            _q("text", "anchor-type"): "paragraph",
            _q("draw", "z-index"): "1",
        },
    )


@dataclass
class _Totals:
    trip_count: int
    distance_nm: float
    fuel_liters: float
    fuel_liters_device: Optional[float]
    engine_hours: Dict[int, float]


def _compute_totals(trips: List[TripLeg]) -> _Totals:
    distance_nm = sum(trip.distance_nm for trip in trips)
    fuel_liters = sum(trip.fuel_liters for trip in trips)
    device_values = [trip.fuel_liters_device for trip in trips if trip.fuel_liters_device is not None]
    fuel_liters_device = sum(device_values) if device_values else None
    engine_hours: Dict[int, float] = {}
    for trip in trips:
        for instance, hours in trip.engine_hours.items():
            engine_hours[instance] = engine_hours.get(instance, 0.0) + hours
    return _Totals(
        trip_count=len(trips),
        distance_nm=distance_nm,
        fuel_liters=fuel_liters,
        fuel_liters_device=fuel_liters_device,
        engine_hours=engine_hours,
    )


def _build_automatic_styles() -> Element:
    styles = Element(_q("office", "automatic-styles"))

    def _column_style(name: str, width_cm: float) -> None:
        style = SubElement(styles, _q("style", "style"), {_q("style", "name"): name, _q("style", "family"): "table-column"})
        SubElement(style, _q("style", "table-column-properties"), {_q("style", "column-width"): f"{width_cm}cm"})

    _column_style("coDefault", _DEFAULT_COLUMN_WIDTH_CM)
    _column_style("coRoute", _ROUTE_COLUMN_WIDTH_CM)

    row_style = SubElement(styles, _q("style", "style"), {_q("style", "name"): "roThumb", _q("style", "family"): "table-row"})
    SubElement(
        row_style,
        _q("style", "table-row-properties"),
        {_q("style", "row-height"): f"{_ROW_HEIGHT_CM}cm", _q("style", "use-optimal-row-height"): "false"},
    )

    line_style = SubElement(styles, _q("style", "style"), {_q("style", "name"): "grLine", _q("style", "family"): "graphic"})
    SubElement(
        line_style,
        _q("style", "graphic-properties"),
        {
            _q("draw", "stroke"): "solid",
            _q("svg", "stroke-color"): _ROUTE_LINE_COLOR,
            _q("svg", "stroke-width"): "0.06cm",
            _q("draw", "fill"): "none",
        },
    )

    return styles


def _build_logbook_table(
    trips: List[TripLeg],
    routes_filename: Optional[str],
    utc_offset_hours: Optional[float],
    use_route_thumbnails: bool,
    tile_cache_dir: Path,
    pictures: List[Tuple[str, bytes]],
) -> Element:
    table = Element(_q("table", "table"), {_q("table", "name"): "Logbook"})
    SubElement(
        table,
        _q("table", "table-column"),
        {_q("table", "style-name"): "coDefault", _q("table", "number-columns-repeated"): str(len(_HEADERS) - 1)},
    )
    SubElement(table, _q("table", "table-column"), {_q("table", "style-name"): "coRoute"})

    header = _row()
    for name in _HEADERS:
        _str_cell(header, name)
    table.append(header)

    for idx, trip in enumerate(trips):
        offset = _trip_utc_offset_hours(trip, utc_offset_hours)
        depart_local = _to_local(trip.depart_time, offset)
        arrive_local = _to_local(trip.arrive_time, offset)
        duration_h = trip.duration.total_seconds() / 3600.0
        avg_consumption_h = trip.fuel_liters / duration_h if duration_h > 0 else None
        avg_consumption_nm = _avg_consumption_l_per_nm(trip)

        row = _row()
        row.set(_q("table", "style-name"), "roThumb")
        _str_cell(row, depart_local.date().isoformat())
        _str_cell(row, depart_local.strftime("%H:%M"))
        _str_cell(row, trip.depart_place)
        _str_cell(row, arrive_local.strftime("%H:%M"))
        _str_cell(row, trip.arrive_place)
        _str_cell(row, _format_duration(trip.duration))
        _num_cell(row, trip.distance_nm)
        _num_cell(row, trip.avg_speed_kn)
        _num_cell(row, trip.max_speed_kn)
        _num_cell(row, trip.fuel_liters)
        _num_cell(row, trip.fuel_liters_device)
        _num_cell(row, avg_consumption_h)
        _num_cell(row, avg_consumption_nm, decimals=2)
        _str_cell(row, _engine_hours_text(trip))
        _str_cell(row, _format_engine_health(trip.engine_health))
        _str_cell(row, _format_warnings(trip.engine_health))
        _num_cell(row, trip.min_depth_m)

        href = f"{routes_filename}#{trip_anchor(idx)}" if trip.track and routes_filename else None
        picture_name = None
        points_px = None
        if use_route_thumbnails and trip.track:
            thumbnail = build_route_thumbnail(trip.track, tile_cache_dir)
            if thumbnail is not None:
                picture_name = f"tile_{idx}.png"
                pictures.append((picture_name, thumbnail.png_bytes))
                points_px = thumbnail.points_px
        _map_cell(row, href, picture_name, points_px)

        table.append(row)

    return table


def _build_totals_table(totals: _Totals) -> Element:
    table = Element(_q("table", "table"), {_q("table", "name"): "Totals"})

    def _kv_row(label: str, value: Optional[float], decimals: int = 1) -> None:
        row = _row()
        _str_cell(row, label)
        _num_cell(row, value, decimals)
        table.append(row)

    _kv_row("Number of trips", totals.trip_count, decimals=0)
    _kv_row("Total distance (nm)", totals.distance_nm)
    _kv_row("Total fuel, calculated (L)", totals.fuel_liters)
    if totals.fuel_liters_device is not None:
        _kv_row("Total fuel, engine meter (L)", totals.fuel_liters_device)
    for instance, hours in sorted(totals.engine_hours.items()):
        _kv_row(f"Total engine hours, engine {instance}", hours)

    return table


def write_ods(
    trips: Iterable[TripLeg],
    path: Path,
    utc_offset_hours: Optional[float] = None,
    routes_filename: Optional[str] = None,
    use_route_thumbnails: bool = True,
    tile_cache_dir: Path = DEFAULT_CACHE_DIR,
) -> None:
    trips = list(trips)
    pictures: List[Tuple[str, bytes]] = []

    root = Element(_q("office", "document-content"), {_q("office", "version"): "1.2"})
    root.append(_build_automatic_styles())
    body = SubElement(root, _q("office", "body"))
    spreadsheet = SubElement(body, _q("office", "spreadsheet"))
    spreadsheet.append(
        _build_logbook_table(trips, routes_filename, utc_offset_hours, use_route_thumbnails, tile_cache_dir, pictures)
    )
    spreadsheet.append(_build_totals_table(_compute_totals(trips)))

    content_xml = tostring(root, encoding="utf-8", xml_declaration=True)

    manifest_entries = [
        f'  <manifest:file-entry manifest:full-path="/" manifest:version="1.2" '
        f'manifest:media-type="{_MIMETYPE}"/>',
        '  <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>',
    ]
    for picture_name, _ in pictures:
        manifest_entries.append(
            f'  <manifest:file-entry manifest:full-path="Pictures/{picture_name}" '
            f'manifest:media-type="image/png"/>'
        )
    manifest_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
        'manifest:version="1.2">\n' + "\n".join(manifest_entries) + "\n</manifest:manifest>\n"
    ).encode("utf-8")

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", _MIMETYPE, compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/manifest.xml", manifest_xml, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("content.xml", content_xml, compress_type=zipfile.ZIP_DEFLATED)
        for picture_name, png_bytes in pictures:
            zf.writestr(f"Pictures/{picture_name}", png_bytes, compress_type=zipfile.ZIP_STORED)
