from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from nmea2000processor.gpx_writer import write_gpx
from nmea2000processor.tripbuilder import EngineHealth, NavSample, TripLeg

_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


def _sample(minute: int, lat: float, lon: float, depth_m=None) -> NavSample:
    return NavSample(datetime(2026, 7, 15, 9, minute), lat, lon, 3.0, depth_m)


def _trip(**overrides) -> TripLeg:
    defaults = dict(
        depart_time=datetime(2026, 7, 15, 9, 0),
        arrive_time=datetime(2026, 7, 15, 9, 30),
        depart_place="Marina A",
        arrive_place="Marina B",
        duration=timedelta(minutes=30),
        distance_nm=6.3,
        avg_speed_kn=None,
        max_speed_kn=None,
        fuel_liters=4.0,
        fuel_liters_device=None,
        engine_hours={},
        engine_hours_total={},
        engine_health={},
        battery_health={},
        typical_rpm={},
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


def test_write_gpx_basic(tmp_path: Path):
    track = [_sample(11, 52.30, 4.90), _sample(20, 52.32, 4.93), _sample(31, 52.34, 4.95)]
    trip = _trip(
        depart_time=track[0].time,
        arrive_time=track[-1].time,
        avg_speed_kn=5.8,
        max_speed_kn=6.1,
        fuel_liters_device=3.8,
        engine_hours={0: 0.5},
        engine_health={0: EngineHealth(None, None, None, None, None, frozenset({"Low Oil Pressure"}))},
        min_depth_m=3.1,
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
    assert "Duration" in desc
    assert "Speed: avg 5,8 kn, max 6,1 kn" in desc
    assert "Fuel (calculated): 4,0 L" in desc
    assert "Fuel (engine meter): 3,8 L" in desc
    assert "Engine hours: 0,5 h" in desc  # no "engine 0:" label with just one engine
    assert "Warnings: Low Oil Pressure" in desc
    assert "Min. depth: 3,1 m" in desc

    points = trk.findall("gpx:trkseg/gpx:trkpt", _NS)
    assert len(points) == 3
    assert points[0].get("lat") == "52.3000000"
    assert points[0].get("lon") == "4.9000000"
    assert points[0].find("gpx:time", _NS).text == "2026-07-15T09:11:00Z"


def test_write_gpx_includes_roll_pitch_variation(tmp_path: Path):
    track = [_sample(11, 52.30, 4.90)]
    trip = _trip(
        roll_variation_deg=2.4, pitch_variation_deg=0.9,
        roll_range_deg=23.0, pitch_range_deg=5.3, track=track,
    )
    out_path = tmp_path / "logboek.gpx"

    write_gpx([trip], out_path)

    tree = ET.parse(out_path)
    desc = tree.getroot().find("gpx:trk/gpx:desc", _NS).text

    assert "Roll variation: ±2,4°, peak 23,0°" in desc
    assert "Pitch variation: ±0,9°, peak 5,3°" in desc


def test_write_gpx_name_uses_local_time(tmp_path: Path):
    track = [_sample(11, 45.0, 26.0), _sample(31, 45.0, 26.0)]  # ~Romania, UTC+2 solar + 1h EU DST in July
    trip = _trip(depart_time=track[0].time, arrive_time=track[-1].time, track=track)
    out_path = tmp_path / "logboek.gpx"

    write_gpx([trip], out_path)

    tree = ET.parse(out_path)
    name = tree.getroot().find("gpx:trk/gpx:name", _NS).text
    assert name.startswith("2026-07-15 12:11")  # 09:11 UTC + 3h estimated (incl. EU DST)
    # <trkpt><time> stays strictly UTC, regardless of the estimated offset used for the name.
    point_time = tree.getroot().find("gpx:trk/gpx:trkseg/gpx:trkpt/gpx:time", _NS).text
    assert point_time == "2026-07-15T09:11:00Z"


def test_write_gpx_name_uses_fixed_utc_offset(tmp_path: Path):
    track = [_sample(11, 45.0, 26.0), _sample(31, 45.0, 26.0)]
    trip = _trip(depart_time=track[0].time, arrive_time=track[-1].time, track=track)
    out_path = tmp_path / "logboek.gpx"

    write_gpx([trip], out_path, utc_offset_hours=-1.0)

    tree = ET.parse(out_path)
    name = tree.getroot().find("gpx:trk/gpx:name", _NS).text
    assert name.startswith("2026-07-15 08:11")


def test_write_gpx_skips_trips_without_track(tmp_path: Path):
    trip = _trip(distance_nm=1.0, fuel_liters=1.0)
    out_path = tmp_path / "logboek.gpx"

    write_gpx([trip], out_path)

    tree = ET.parse(out_path)
    assert tree.getroot().findall("gpx:trk", _NS) == []
