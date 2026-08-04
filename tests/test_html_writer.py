from datetime import datetime, timedelta
from pathlib import Path

from nmea2000processor.html_writer import write_html_logbook
from nmea2000processor.tripbuilder import NavSample, TripLeg


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
        engine_hours_total={0: 123.4},
        engine_health={},
        min_depth_m=None,
        min_depth_lat=None,
        min_depth_lon=None,
        track=[],
    )
    defaults.update(overrides)
    return TripLeg(**defaults)


def test_write_html_logbook_includes_boat_name_in_title_and_heading(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path, boat_name="Zeevalk")

    html = out_path.read_text(encoding="utf-8")
    assert "<title>Zeevalk - Sailing Logbook</title>" in html
    assert "Zeevalk" in html and "Sailing Logbook" in html


def test_write_html_logbook_without_boat_name(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path, boat_name=None)

    html = out_path.read_text(encoding="utf-8")
    assert "<title>Sailing Logbook</title>" in html


def test_write_html_logbook_shows_totals(tmp_path: Path):
    trip_a = _trip(distance_nm=10.0, fuel_liters=5.0, engine_hours={0: 1.0})
    trip_b = _trip(distance_nm=6.0, fuel_liters=3.0, engine_hours={0: 0.5, 1: 2.0})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_a, trip_b], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "16,0 nm" in html  # total distance
    assert "8,0 L" in html  # total fuel
    assert "Hours logged, engine 0" in html
    assert "Hours logged, engine 1" in html


def test_write_html_logbook_totals_omit_engine_label_with_one_engine(tmp_path: Path):
    trip = _trip(engine_hours={0: 1.5})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "Hours logged</div>" in html
    assert "engine 0" not in html.lower()


def test_write_html_logbook_shows_current_engine_hour_meter(tmp_path: Path):
    # a later trip's reading should win over an earlier one, since it's the most recent
    trip_a = _trip(
        depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0),
        engine_hours_total={0: 100.0},
    )
    trip_b = _trip(
        depart_time=datetime(2026, 7, 16, 9, 0), arrive_time=datetime(2026, 7, 16, 10, 0),
        engine_hours_total={0: 102.5},
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_a, trip_b], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "Engine hour meter</div>" in html
    assert "102,5 h" in html
    assert "100,0 h" not in html


def test_write_html_logbook_groups_by_year_and_week(tmp_path: Path):
    trip_2025 = _trip(depart_time=datetime(2025, 6, 10, 9, 0), arrive_time=datetime(2025, 6, 10, 10, 0))
    trip_2026 = _trip(depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_2025, trip_2026], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "<h2>2025</h2>" in html
    assert "<h2>2026</h2>" in html
    # 2026 is a more recent year and must appear before 2025 in the document
    assert html.index("<h2>2026</h2>") < html.index("<h2>2025</h2>")


def test_write_html_logbook_map_button_only_with_track(tmp_path: Path):
    track = [NavSample(datetime(2026, 7, 15, 9, 0), 52.30, 4.90, 3.0, None)]
    trip_with_track = _trip(track=track)
    trip_without_track = _trip(depart_time=datetime(2026, 7, 16, 9, 0), arrive_time=datetime(2026, 7, 16, 10, 0))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_with_track, trip_without_track], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert html.count('class="show-map"') == 1
    assert '"points": [[52.3, 4.9]]' in html.replace(" ", "").replace("\n", "") or "52.3" in html


def test_write_html_logbook_embeds_trip_uid_as_data_attribute(tmp_path: Path):
    trip_a = _trip(depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0))
    trip_b = _trip(depart_time=datetime(2026, 7, 16, 9, 0), arrive_time=datetime(2026, 7, 16, 10, 0))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_a, trip_b], out_path, trip_uids=["uid-a", "uid-b"])

    html = out_path.read_text(encoding="utf-8")
    assert 'data-uid="uid-a"' in html
    assert 'data-uid="uid-b"' in html


def test_write_html_logbook_without_trip_uids(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path)  # trip_uids omitted entirely

    html = out_path.read_text(encoding="utf-8")
    assert "data-uid" not in html


def test_write_html_logbook_shows_remarks(tmp_path: Path):
    trip_a = _trip(depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0))
    trip_b = _trip(depart_time=datetime(2026, 7, 16, 9, 0), arrive_time=datetime(2026, 7, 16, 10, 0))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_a, trip_b], out_path, remarks=["Nice broad reach", ""])

    html = out_path.read_text(encoding="utf-8")
    assert "Nice broad reach" in html


def test_write_html_logbook_escapes_place_names(tmp_path: Path):
    trip = _trip(depart_place="Marina <A> & Co")
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "Marina <A> & Co" not in html
    assert "&lt;A&gt;" in html and "&amp;" in html
