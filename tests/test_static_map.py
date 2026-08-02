from datetime import datetime, timedelta

from nmea2000processor.static_map import _choose_tile, _lonlat_to_tile_xy, build_route_thumbnail
from nmea2000processor.tripbuilder import NavSample


def test_lonlat_to_tile_xy_matches_known_reference():
    # Greenwich/equator op zoom 0 moet in het midden van de enige tegel vallen.
    x, y = _lonlat_to_tile_xy(0.0, 0.0, 0)
    assert x == 0.5
    assert 0.4 < y < 0.6


def test_choose_tile_picks_high_zoom_for_a_small_area():
    # Twee punten enkele meters uit elkaar -> moet op hoge zoom nog in 1 tegel passen.
    track = [
        NavSample(datetime(2026, 7, 15, 9, 0), 47.8704, -3.9147, 0.0, None),
        NavSample(datetime(2026, 7, 15, 9, 1), 47.8706, -3.9149, 0.0, None),
    ]
    zoom, x, y = _choose_tile(track)
    assert zoom >= 14


def test_choose_tile_falls_back_to_low_zoom_for_a_wide_area():
    track = [
        NavSample(datetime(2026, 7, 15, 9, 0), 40.0, -10.0, 0.0, None),
        NavSample(datetime(2026, 7, 15, 9, 1), 55.0, 20.0, 0.0, None),
    ]
    zoom, x, y = _choose_tile(track)
    assert zoom <= 6


def test_build_route_thumbnail_returns_none_when_tile_fetch_fails(monkeypatch, tmp_path):
    import nmea2000processor.static_map as static_map

    monkeypatch.setattr(static_map, "_fetch_tile", lambda zoom, x, y, cache_dir: None)
    track = [NavSample(datetime(2026, 7, 15, 9, 0), 47.87, -3.91, 0.0, None)]

    assert build_route_thumbnail(track, cache_dir=tmp_path) is None


def test_build_route_thumbnail_returns_none_for_empty_track(tmp_path):
    assert build_route_thumbnail([], cache_dir=tmp_path) is None


def test_build_route_thumbnail_decimates_points_and_keeps_last(monkeypatch, tmp_path):
    import nmea2000processor.static_map as static_map

    monkeypatch.setattr(static_map, "_fetch_tile", lambda zoom, x, y, cache_dir: b"fake-png-bytes")
    base = datetime(2026, 7, 15, 9, 0)
    track = [
        NavSample(base + timedelta(seconds=m), 47.8700 + m * 0.00001, -3.9147, 0.0, None)
        for m in range(500)
    ]

    thumbnail = static_map.build_route_thumbnail(track, cache_dir=tmp_path)

    assert thumbnail is not None
    assert thumbnail.png_bytes == b"fake-png-bytes"
    assert len(thumbnail.points_px) < len(track)
    # laatste puntje van de track moet altijd zijn meegenomen, ongeacht de decimatie-stride
    last_expected = static_map._lonlat_to_tile_xy(track[-1].lon, track[-1].lat, static_map._choose_tile(track)[0])
    tile_x, tile_y = static_map._choose_tile(track)[1], static_map._choose_tile(track)[2]
    expected_px = ((last_expected[0] - tile_x) * static_map.TILE_SIZE, (last_expected[1] - tile_y) * static_map.TILE_SIZE)
    assert thumbnail.points_px[-1] == expected_px
