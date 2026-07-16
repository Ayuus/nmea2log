import csv
from datetime import datetime
from pathlib import Path

from nmea2000processor.logbook_writer import write_csv
from nmea2000processor.tripbuilder import TripLeg


def test_write_csv_basic(tmp_path: Path):
    trip = TripLeg(
        depart_time=datetime(2026, 7, 15, 9, 0),
        arrive_time=datetime(2026, 7, 15, 10, 30),
        depart_place="Marina A",
        arrive_place="Marina B",
        distance_nm=12.3,
        fuel_liters=9.75,
        engine_hours={0: 1.5},
    )
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
    assert row["brandstof_L"] == "9,8"
    assert row["draaiuren"] == "motor 0: 1,5 u"
