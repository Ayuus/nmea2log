from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from nmea2000processor.gpx_writer import write_gpx
from nmea2000processor.tripbuilder import NavSample, TripLeg

_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


def _sample(minute: int, lat: float, lon: float) -> NavSample:
    return NavSample(datetime(2026, 7, 15, 9, minute), lat, lon, 3.0)


def test_write_gpx_basic(tmp_path: Path):
    track = [_sample(11, 52.30, 4.90), _sample(20, 52.32, 4.93), _sample(31, 52.34, 4.95)]
    trip = TripLeg(
        depart_time=track[0].time,
        arrive_time=track[-1].time,
        depart_place="Marina A",
        arrive_place="Marina B",
        distance_nm=6.3,
        fuel_liters=4.0,
        fuel_liters_device=3.8,
        engine_hours={0: 0.5},
        track=track,
    )
    out_path = tmp_path / "logboek.gpx"

    write_gpx([trip], out_path)

    tree = ET.parse(out_path)
    root = tree.getroot()
    tracks = root.findall("gpx:trk", _NS)
    assert len(tracks) == 1

    trk = tracks[0]
    name = trk.find("gpx:name", _NS).text
    assert "Marina A" in name and "Marina B" in name

    desc = trk.find("gpx:desc", _NS).text
    assert "Vaartijd" in desc
    assert "Brandstof (berekend): 4,0 L" in desc
    assert "Brandstof (motorteller): 3,8 L" in desc
    assert "motor 0: 0,5 u" in desc

    points = trk.findall("gpx:trkseg/gpx:trkpt", _NS)
    assert len(points) == 3
    assert points[0].get("lat") == "52.3000000"
    assert points[0].get("lon") == "4.9000000"
    assert points[0].find("gpx:time", _NS).text == "2026-07-15T09:11:00Z"


def test_write_gpx_skips_trips_without_track(tmp_path: Path):
    trip = TripLeg(
        depart_time=datetime(2026, 7, 15, 9, 0),
        arrive_time=datetime(2026, 7, 15, 9, 30),
        depart_place="A",
        arrive_place="B",
        distance_nm=1.0,
        fuel_liters=1.0,
        fuel_liters_device=None,
        engine_hours={},
        track=[],
    )
    out_path = tmp_path / "logboek.gpx"

    write_gpx([trip], out_path)

    tree = ET.parse(out_path)
    assert tree.getroot().findall("gpx:trk", _NS) == []
