import csv
import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from nmea2log.cli import main

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
DEMO = EXAMPLES / "demo-data" / "Actisense"


def _load(name: str):
    sys.path.insert(0, str(EXAMPLES))
    try:
        spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(EXAMPLES))


def test_demo_files_are_what_the_generator_makes(tmp_path):
    # Fails after a change to examples/generate_demo_ebl.py or demo_cruise.py until the files have been made again
    # (python examples/generate_demo_ebl.py).
    generator = _load("generate_demo_ebl")

    paths = generator.write_all(tmp_path)

    assert len(paths) == 5
    for path in paths:
        committed = DEMO / "EBL000001" / path.name
        assert committed.read_bytes() == path.read_bytes(), path.name


def test_demo_files_are_small_enough_to_live_in_the_repository():
    assert sum(path.stat().st_size for path in DEMO.rglob("*.ebl")) < 4_000_000


def test_importing_the_demo_files_builds_the_trips_of_the_demo_cruise(tmp_path, monkeypatch):
    cruise = _load("demo_cruise")
    monkeypatch.chdir(tmp_path)  # no nmea2log.ini here, nothing to upload to

    exit_code = main(
        ["--no-upload", "--no-geocode", "--no-weather", "--no-marine", "--csv", "-o", str(tmp_path / "logbook.csv"), "--ebl-dir", str(DEMO)]
    )

    assert exit_code == 0
    with open(tmp_path / "logbook.csv", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    assert len(rows) == len(cruise.DURATIONS_H) == 5
    for leg, row in enumerate(rows):
        depart, _ = cruise.leg_times(leg)
        assert row["date"] == depart.date().isoformat()
        assert float(row["distance_nm"].replace(",", ".")) == pytest.approx(cruise.leg_distance_nm(leg), rel=0.03)
        hours, minutes = row["duration"].split(":")
        assert int(hours) * 60 + int(minutes) == pytest.approx(cruise.DURATIONS_H[leg] * 60, abs=4)
        assert float(row["fuel_L_calculated"].replace(",", ".")) == pytest.approx(cruise.leg_distance_nm(leg) * cruise.FUEL_PER_NM, rel=0.08)
        assert float(row["min_depth_m"].replace(",", ".")) == pytest.approx(cruise.MIN_DEPTH_M[leg], abs=0.1)
        assert row["typical_rpm"].strip() != ""


def test_demo_cruise_distances_are_the_ones_of_its_route():
    cruise = _load("demo_cruise")

    # West-Terschelling to Enkhuizen is the long way round through the Kornwerderzand lock
    assert cruise.leg_distance_nm(4) > 38
    assert cruise.leg_times(1)[0] - cruise.leg_times(0)[1] == timedelta(hours=cruise.OVERNIGHT_H)


def test_the_zip_holds_the_demo_folder_and_is_made_the_same_every_time(tmp_path):
    import zipfile

    generator = _load("generate_demo_ebl")
    source = tmp_path / "files"
    generator.write_all(source)

    generator.write_zip(tmp_path / "a.zip", source)
    generator.write_zip(tmp_path / "b.zip", source)

    assert (tmp_path / "a.zip").read_bytes() == (tmp_path / "b.zip").read_bytes()
    with zipfile.ZipFile(tmp_path / "a.zip") as archive:
        assert archive.namelist() == [f"Actisense/EBL000001/000001_00{n}.ebl" for n in range(1, 6)]
        assert archive.read("Actisense/EBL000001/000001_001.ebl") == (DEMO / "EBL000001" / "000001_001.ebl").read_bytes()
