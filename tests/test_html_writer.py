from datetime import datetime, timedelta
from pathlib import Path

from nmea2000processor.html_writer import write_html_logbook
from nmea2000processor.tripbuilder import BatteryHealth, NavSample, TripLeg


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
        battery_health={},
        typical_rpm={},
        typical_rpm_speed_kn={},
        min_depth_m=None,
        min_depth_lat=None,
        min_depth_lon=None,
        avg_water_temp_c=None,
        min_water_temp_c=None,
        max_water_temp_c=None,
        roll_variation_deg=None,
        pitch_variation_deg=None,
        roll_range_deg=None,
        pitch_range_deg=None,
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


def test_write_html_logbook_shows_hours_underway_and_avg_speed(tmp_path: Path):
    trip_a = _trip(distance_nm=10.0, duration=timedelta(hours=2))
    trip_b = _trip(distance_nm=6.0, duration=timedelta(hours=2))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_a, trip_b], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "Total hours" in html
    assert "4,0 h" in html  # 2h + 2h
    assert "Avg speed" in html
    assert "4,0 kn" in html  # 16 nm / 4 h, distance-weighted


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


def test_write_html_logbook_one_table_per_year_with_week_divider_rows(tmp_path: Path):
    """Regression test: weeks used to each get their own <table>, so columns from different
    weeks could end up different widths and not line up. All of a year's trips must now share a
    single table (so column widths are computed together), with a divider row between weeks
    instead of a separate table."""
    week_a = _trip(depart_time=datetime(2026, 7, 6, 9, 0), arrive_time=datetime(2026, 7, 6, 10, 0))
    week_b = _trip(depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0))
    out_path = tmp_path / "logbook.html"

    write_html_logbook([week_a, week_b], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert html.count('<table class="trips">') == 1
    assert html.count('class="week-row"') == 2
    assert "Week 28" in html and "Week 29" in html


def test_write_html_logbook_shows_per_year_totals(tmp_path: Path):
    trip_2025 = _trip(
        depart_time=datetime(2025, 6, 10, 9, 0), arrive_time=datetime(2025, 6, 10, 10, 0), distance_nm=7.0,
    )
    trip_2026a = _trip(
        depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0), distance_nm=10.0,
    )
    trip_2026b = _trip(
        depart_time=datetime(2026, 8, 1, 9, 0), arrive_time=datetime(2026, 8, 1, 10, 0), distance_nm=6.0,
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_2025, trip_2026a, trip_2026b], out_path)

    html = out_path.read_text(encoding="utf-8")
    # overall total (23.0) and the 2026-only total (16.0) must both appear
    assert "23,0 nm" in html
    assert "16,0 nm" in html
    # the per-year total must land after that year's heading, not before it
    assert html.index("<h2>2026</h2>") < html.index("16,0 nm")


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


def test_write_html_logbook_shows_max_speed_per_trip_and_overall(tmp_path: Path):
    trip_a = _trip(
        depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 0),
        avg_speed_kn=5.0, max_speed_kn=8.2,
    )
    trip_b = _trip(
        depart_time=datetime(2026, 7, 16, 9, 0), arrive_time=datetime(2026, 7, 16, 10, 0),
        avg_speed_kn=6.0, max_speed_kn=11.6,
    )
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip_a, trip_b], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "8,2 kn" in html
    assert "11,6 kn" in html
    assert "Top speed</div>" in html


def test_write_html_logbook_shows_low_battery_warning(tmp_path: Path):
    trip = _trip(battery_health={0: BatteryHealth(avg_voltage_v=12.6, min_voltage_v=11.8)})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path, battery_warning_voltage=12.2)

    html = out_path.read_text(encoding="utf-8")
    assert "low battery 11,8 V" in html


def test_write_html_logbook_shows_water_temp_badge(tmp_path: Path):
    trip = _trip(avg_water_temp_c=21.8, min_water_temp_c=21.7, max_water_temp_c=21.9)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "temp-hover" in html
    assert "21,8°C" in html
    assert "#EAF3DE" in html  # green bucket for 18-22°C


def test_write_html_logbook_shows_speed_at_typical_rpm_tooltip(tmp_path: Path):
    trip = _trip(typical_rpm={0: 2250.0}, typical_rpm_speed_kn={0: (12.6, 13.4, 13.0)})
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "2250" in html
    assert "avg 13,0 kn at that RPM (12,6-13,4 kn)" in html


def test_write_html_logbook_shows_motion_variation(tmp_path: Path):
    trip = _trip(roll_variation_deg=2.5, pitch_variation_deg=0.7)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "roll ±2,5°" in html
    assert "pitch ±0,7°" in html


def test_write_html_logbook_shows_motion_peak_in_tooltip(tmp_path: Path):
    """The standard deviation alone can look deceptively small for a trip that's mostly calm
    with one rough patch, so the peak-to-peak range shows up too (in a hover tooltip, same
    pattern as the water-temp badge, to keep the visible cell compact)."""
    trip = _trip(roll_variation_deg=2.5, pitch_variation_deg=0.7, roll_range_deg=23.0, pitch_range_deg=5.3)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "roll ±2,5°" in html
    assert "roll peak 23,0°" in html
    assert "pitch peak 5,3°" in html


def test_write_html_logbook_water_temp_badge_shows_range_when_notable(tmp_path: Path):
    trip = _trip(avg_water_temp_c=17.0, min_water_temp_c=15.0, max_water_temp_c=19.0)
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "15,0" in html and "19,0" in html


def test_write_html_logbook_no_water_temp_badge_without_data(tmp_path: Path):
    out_path = tmp_path / "logbook.html"

    write_html_logbook([_trip()], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert 'class="temp-hover"' not in html


def test_write_html_logbook_escapes_place_names(tmp_path: Path):
    trip = _trip(depart_place="Marina <A> & Co")
    out_path = tmp_path / "logbook.html"

    write_html_logbook([trip], out_path)

    html = out_path.read_text(encoding="utf-8")
    assert "Marina <A> & Co" not in html
    assert "&lt;A&gt;" in html and "&amp;" in html
