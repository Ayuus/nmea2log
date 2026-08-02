import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from nmea2000processor.ods_writer import write_ods
from nmea2000processor.tripbuilder import NavSample, TripLeg

_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
}


def _trip(**overrides) -> TripLeg:
    defaults = dict(
        depart_time=datetime(2026, 7, 15, 9, 0),
        arrive_time=datetime(2026, 7, 15, 10, 30),
        depart_place="Marina A",
        arrive_place="Marina B",
        duration=timedelta(hours=1, minutes=30),
        distance_nm=12.0,
        avg_speed_kn=None,
        max_speed_kn=None,
        fuel_liters=6.0,
        fuel_liters_device=None,
        engine_hours={0: 1.5},
        engine_health={},
        min_depth_m=None,
        min_depth_lat=None,
        min_depth_lon=None,
        track=[],
    )
    defaults.update(overrides)
    return TripLeg(**defaults)


def _read_content_xml(path: Path) -> ET.Element:
    with zipfile.ZipFile(path) as zf:
        assert zf.read("mimetype") == b"application/vnd.oasis.opendocument.spreadsheet"
        content_bytes = zf.read("content.xml")
    return ET.fromstring(content_bytes)


def _tables(root: ET.Element):
    return root.findall(".//table:table", _NS)


def test_write_ods_is_a_valid_zip_with_two_sheets(tmp_path: Path):
    trip = _trip()
    out_path = tmp_path / "logbook.ods"

    write_ods([trip], out_path, use_route_thumbnails=False)

    assert zipfile.is_zipfile(out_path)
    root = _read_content_xml(out_path)
    tables = _tables(root)
    names = [t.get(f"{{{_NS['table']}}}name") for t in tables]
    assert names == ["Logbook", "Totals"]


def test_logbook_sheet_has_english_headers_and_row_values(tmp_path: Path):
    trip = _trip()
    out_path = tmp_path / "logbook.ods"

    write_ods([trip], out_path, use_route_thumbnails=False)

    root = _read_content_xml(out_path)
    logbook_table = _tables(root)[0]
    rows = logbook_table.findall("table:table-row", _NS)
    header_cells = rows[0].findall("table:table-cell/text:p", _NS)
    headers = [cell.text for cell in header_cells]
    assert headers[0] == "date"
    assert "route" in headers

    data_cells = rows[1].findall("table:table-cell", _NS)
    port_cell_text = data_cells[2].find("text:p", _NS).text
    assert port_cell_text == "Marina A"


def test_totals_sheet_sums_across_trips(tmp_path: Path):
    trip_a = _trip(distance_nm=10.0, fuel_liters=5.0, engine_hours={0: 1.0})
    trip_b = _trip(distance_nm=6.0, fuel_liters=3.0, engine_hours={0: 0.5, 1: 2.0})
    out_path = tmp_path / "logbook.ods"

    write_ods([trip_a, trip_b], out_path, use_route_thumbnails=False)

    root = _read_content_xml(out_path)
    totals_table = _tables(root)[1]
    rows = totals_table.findall("table:table-row", _NS)
    label_to_value = {}
    for row in rows:
        cells = row.findall("table:table-cell", _NS)
        label = cells[0].find("text:p", _NS).text
        value_attr = cells[1].get(f"{{{_NS['office']}}}value")
        label_to_value[label] = value_attr

    assert label_to_value["Number of trips"] == "2.0"
    assert label_to_value["Total distance (nm)"] == "16.0"
    assert label_to_value["Total fuel, calculated (L)"] == "8.0"
    assert label_to_value["Total engine hours, engine 0"] == "1.5"
    assert label_to_value["Total engine hours, engine 1"] == "2.0"


def test_route_column_links_to_routes_file_when_track_present(tmp_path: Path):
    track = [NavSample(datetime(2026, 7, 15, 9, 0), 47.87, -3.91, 3.0, None)]
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.ods"

    write_ods([trip], out_path, routes_filename="logbook_routes.html", use_route_thumbnails=False)

    root = _read_content_xml(out_path)
    logbook_table = _tables(root)[0]
    rows = logbook_table.findall("table:table-row", _NS)
    route_cell = rows[1].findall("table:table-cell", _NS)[-1]
    link = route_cell.find("text:p/text:a", _NS)
    assert link is not None
    assert link.get(f"{{{_NS['xlink']}}}href") == "logbook_routes.html#trip-0"


def test_route_column_blank_without_track_or_routes_filename(tmp_path: Path):
    trip = _trip(track=[])
    out_path = tmp_path / "logbook.ods"

    write_ods([trip], out_path, routes_filename="logbook_routes.html", use_route_thumbnails=False)

    root = _read_content_xml(out_path)
    logbook_table = _tables(root)[0]
    rows = logbook_table.findall("table:table-row", _NS)
    route_cell = rows[1].findall("table:table-cell", _NS)[-1]
    assert route_cell.find("text:p/text:a", _NS) is None


def test_route_thumbnail_embedded_when_tile_fetch_succeeds(tmp_path: Path, monkeypatch):
    import nmea2000processor.ods_writer as ods_writer

    monkeypatch.setattr(
        ods_writer,
        "build_route_thumbnail",
        lambda track, cache_dir: ods_writer_module_thumbnail(),
    )
    track = [NavSample(datetime(2026, 7, 15, 9, 0), 47.87, -3.91, 3.0, None)]
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.ods"

    write_ods([trip], out_path, routes_filename="logbook_routes.html", use_route_thumbnails=True)

    with zipfile.ZipFile(out_path) as zf:
        names = zf.namelist()
        assert "Pictures/tile_0.png" in names
        assert zf.read("Pictures/tile_0.png") == b"fake-tile-bytes"

    root = _read_content_xml(out_path)
    logbook_table = _tables(root)[0]
    rows = logbook_table.findall("table:table-row", _NS)
    route_cell = rows[1].findall("table:table-cell", _NS)[-1]
    assert route_cell.find(".//{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}polyline") is not None


def ods_writer_module_thumbnail():
    from nmea2000processor.static_map import RouteThumbnail

    return RouteThumbnail(png_bytes=b"fake-tile-bytes", points_px=[(10.0, 20.0), (30.0, 40.0)])
