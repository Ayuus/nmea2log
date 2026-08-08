import csv
from datetime import datetime, timedelta
from pathlib import Path

from nmea2000processor.logbook_writer import _is_eu_dst, write_csv
from nmea2000processor.tripbuilder import BatteryHealth, EngineHealth, NavSample, TripLeg


def _trip(**overrides) -> TripLeg:
    defaults = dict(
        depart_time=datetime(2026, 7, 15, 9, 0),
        arrive_time=datetime(2026, 7, 15, 10, 30),
        depart_place="Marina A",
        arrive_place="Marina B",
        duration=timedelta(hours=1, minutes=30),
        distance_nm=12.3,
        avg_speed_kn=None,
        max_speed_kn=None,
        fuel_liters=9.75,
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


def test_write_csv_basic(tmp_path: Path):
    trip = _trip()
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert len(rows) == 1
    row = rows[0]
    assert row["date"] == "2026-07-15"
    assert row["departure_time"] == "09:00"
    assert row["departure_port"] == "Marina A"
    assert row["arrival_time"] == "10:30"
    assert row["arrival_port"] == "Marina B"
    assert row["duration"] == "1:30"
    assert row["distance_nm"] == "12,3"
    assert row["avg_speed_kn"] == ""
    assert row["max_speed_kn"] == ""
    assert row["fuel_L_calculated"] == "9,8"
    assert row["fuel_L_engine_meter"] == ""
    assert row["avg_consumption_L_per_nm"] == "0,79"
    assert row["engine_hours"] == "1,5 h"  # no "engine 0:" label with just one engine
    assert row["engine_health"] == ""
    assert row["warnings"] == ""
    assert row["min_depth_m"] == ""
    assert row["min_depth_position"] == ""
    assert row["avg_water_temp_c"] == ""
    assert row["min_water_temp_c"] == ""
    assert row["max_water_temp_c"] == ""


def test_write_csv_water_temp_columns(tmp_path: Path):
    trip = _trip(avg_water_temp_c=18.45, min_water_temp_c=17.1, max_water_temp_c=19.8)
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["avg_water_temp_c"] == "18,4"  # float formatting rounds .45 down here
    assert rows[0]["min_water_temp_c"] == "17,1"
    assert rows[0]["max_water_temp_c"] == "19,8"


def test_write_csv_roll_pitch_variation_and_range_columns(tmp_path: Path):
    trip = _trip(roll_variation_deg=2.4, pitch_variation_deg=0.9, roll_range_deg=23.0, pitch_range_deg=5.3)
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["roll_variation_deg"] == "2,4"
    assert rows[0]["pitch_variation_deg"] == "0,9"
    assert rows[0]["roll_range_deg"] == "23,0"
    assert rows[0]["pitch_range_deg"] == "5,3"


def test_write_csv_labels_engines_when_there_are_more_than_one(tmp_path: Path):
    trip = _trip(
        engine_hours={0: 1.5, 1: 1.4},
        engine_health={
            0: EngineHealth(None, None, None, None, None, warnings=frozenset({"Low Oil Pressure"})),
            1: EngineHealth(None, None, None, None, None, warnings=frozenset()),
        },
    )
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    row = rows[0]
    assert row["engine_hours"] == "engine 0: 1,5 h, engine 1: 1,4 h"
    assert row["warnings"] == "engine 0: Low Oil Pressure"


def test_write_csv_typical_rpm_single_engine(tmp_path: Path):
    trip = _trip(typical_rpm={0: 2200.0})
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["typical_rpm"] == "2200"


def test_write_csv_typical_rpm_labels_engines_when_there_are_more_than_one(tmp_path: Path):
    trip = _trip(typical_rpm={0: 2200.0, 1: 1800.0})
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["typical_rpm"] == "engine 0: 2200, engine 1: 1800"


def test_write_csv_typical_rpm_blank_without_data(tmp_path: Path):
    trip = _trip()
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["typical_rpm"] == ""


def test_write_csv_low_battery_warning(tmp_path: Path):
    trip = _trip(battery_health={0: BatteryHealth(avg_voltage_v=12.6, min_voltage_v=11.8)})
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path, battery_warning_voltage=12.2)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert "low battery 11,8 V" in rows[0]["warnings"]


def test_write_csv_no_battery_warning_when_voltage_is_healthy(tmp_path: Path):
    trip = _trip(battery_health={0: BatteryHealth(avg_voltage_v=12.8, min_voltage_v=12.6)})
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path, battery_warning_voltage=12.2)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["warnings"] == ""


def test_write_csv_with_device_fuel(tmp_path: Path):
    trip = _trip(fuel_liters_device=8.0)
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["fuel_L_engine_meter"] == "8,0"


def test_write_csv_engine_health_warnings_and_depth(tmp_path: Path):
    trip = _trip(
        avg_speed_kn=6.2,
        max_speed_kn=8.9,
        engine_health={
            0: EngineHealth(
                oil_pressure_bar_avg=3.2,
                oil_temperature_c_avg=78.0,
                coolant_temperature_c_avg=82.0,
                alternator_voltage_v_avg=14.2,
                engine_load_pct_max=76.0,
                warnings=frozenset({"Low Oil Pressure"}),
            )
        },
        min_depth_m=2.4,
        min_depth_lat=52.3235,
        min_depth_lon=4.9422,
    )
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    row = rows[0]
    assert row["avg_speed_kn"] == "6,2"
    assert row["max_speed_kn"] == "8,9"
    assert "oil 3,2 bar" in row["engine_health"]
    assert "coolant 82°C" in row["engine_health"]
    assert row["warnings"] == "Low Oil Pressure"
    assert row["min_depth_m"] == "2,4"
    assert row["min_depth_position"] == "52.3235, 4.9422"


def test_write_csv_estimates_local_time_from_departure_longitude_in_summer(tmp_path: Path):
    # Longitude 26 (~Romania) -> solar estimate round(26/15) == 2, plus 1 hour because
    # mid-July falls within EU summer time, so the effective offset is UTC+3.
    track = [NavSample(datetime(2026, 7, 15, 9, 0), 45.0, 26.0, 3.0, None)]
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["departure_time"] == "12:00"
    assert rows[0]["arrival_time"] == "13:30"
    assert rows[0]["duration"] == "1:30"  # duration stays offset-independent


def test_write_csv_estimates_local_time_from_departure_longitude_in_winter(tmp_path: Path):
    # Same longitude, but mid-January falls outside EU summer time, so no +1 hour is added.
    track = [NavSample(datetime(2026, 1, 15, 9, 0), 45.0, 26.0, 3.0, None)]
    trip = _trip(depart_time=datetime(2026, 1, 15, 9, 0), arrive_time=datetime(2026, 1, 15, 10, 30), track=track)
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["departure_time"] == "11:00"


def test_write_csv_uses_cet_not_solar_estimate_for_brittany_in_summer(tmp_path: Path):
    # Concarneau, Brittany: solar longitude alone would suggest UTC+0, but France observes
    # CEST (UTC+2) in July -- this is exactly the real-world mismatch the lat/lon-aware
    # estimate exists to fix.
    track = [NavSample(datetime(2026, 7, 30, 8, 36), 47.87, -3.91, 3.0, None)]
    trip = _trip(depart_time=datetime(2026, 7, 30, 8, 36), track=track)
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["departure_time"] == "10:36"


def test_write_csv_uses_wet_not_cet_for_uk_in_summer(tmp_path: Path):
    # Falmouth, UK: similar longitude to Brittany, but the UK observes BST (UTC+1) in July, not
    # CEST (UTC+2) -- latitude is what tells these two apart.
    track = [NavSample(datetime(2026, 7, 30, 8, 36), 50.15, -5.07, 3.0, None)]
    trip = _trip(depart_time=datetime(2026, 7, 30, 8, 36), track=track)
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["departure_time"] == "09:36"


def test_is_eu_dst_boundaries_2026():
    # EU summer time 2026: starts 01:00 UTC on 2026-03-29 (last Sunday of March), ends 01:00 UTC
    # on 2026-10-25 (last Sunday of October).
    assert _is_eu_dst(datetime(2026, 3, 28, 23, 59)) is False
    assert _is_eu_dst(datetime(2026, 3, 29, 1, 0)) is True
    assert _is_eu_dst(datetime(2026, 7, 15, 12, 0)) is True
    assert _is_eu_dst(datetime(2026, 10, 25, 0, 59)) is True
    assert _is_eu_dst(datetime(2026, 10, 25, 1, 0)) is False


def test_write_csv_consumption_per_nm_blank_when_no_distance(tmp_path: Path):
    trip = _trip(distance_nm=0.0)
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["avg_consumption_L_per_nm"] == ""


def test_write_csv_fixed_utc_offset_overrides_estimate(tmp_path: Path):
    track = [NavSample(datetime(2026, 7, 15, 9, 0), 45.0, 26.0, 3.0, None)]
    trip = _trip(track=track)
    out_path = tmp_path / "logbook.csv"

    write_csv([trip], out_path, utc_offset_hours=-1.0)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["departure_time"] == "08:00"
