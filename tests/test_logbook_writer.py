import csv
from datetime import datetime
from pathlib import Path

from nmea2000processor.logbook_writer import write_csv
from nmea2000processor.tripbuilder import EngineHealth, TripLeg


def _trip(**overrides) -> TripLeg:
    defaults = dict(
        depart_time=datetime(2026, 7, 15, 9, 0),
        arrive_time=datetime(2026, 7, 15, 10, 30),
        depart_place="Marina A",
        arrive_place="Marina B",
        distance_nm=12.3,
        avg_speed_kn=None,
        max_speed_kn=None,
        fuel_liters=9.75,
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


def test_write_csv_basic(tmp_path: Path):
    trip = _trip()
    out_path = tmp_path / "logboek.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert len(rows) == 1
    row = rows[0]
    assert row["datum"] == "2026-07-15"
    assert row["vertrektijd"] == "09:00"
    assert row["vertrekhaven"] == "Marina A"
    assert row["aankomsttijd"] == "10:30"
    assert row["aankomsthaven"] == "Marina B"
    assert row["vaartijd"] == "1:30"
    assert row["afstand_nm"] == "12,3"
    assert row["gem_snelheid_kn"] == ""
    assert row["max_snelheid_kn"] == ""
    assert row["brandstof_L_berekend"] == "9,8"
    assert row["brandstof_L_motorteller"] == ""
    assert row["draaiuren"] == "motor 0: 1,5 u"
    assert row["motorgezondheid"] == ""
    assert row["waarschuwingen"] == ""
    assert row["min_diepte_m"] == ""
    assert row["min_diepte_positie"] == ""


def test_write_csv_with_device_fuel(tmp_path: Path):
    trip = _trip(fuel_liters_device=8.0)
    out_path = tmp_path / "logboek.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    assert rows[0]["brandstof_L_motorteller"] == "8,0"


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
    out_path = tmp_path / "logboek.csv"

    write_csv([trip], out_path)

    with out_path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    row = rows[0]
    assert row["gem_snelheid_kn"] == "6,2"
    assert row["max_snelheid_kn"] == "8,9"
    assert "olie 3,2 bar" in row["motorgezondheid"]
    assert "koelvloeistof 82°C" in row["motorgezondheid"]
    assert row["waarschuwingen"] == "motor 0: Low Oil Pressure"
    assert row["min_diepte_m"] == "2,4"
    assert row["min_diepte_positie"] == "52.3235, 4.9422"
