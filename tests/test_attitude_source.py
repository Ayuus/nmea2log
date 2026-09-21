from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nmea2log.attitude_source import AttitudeSegments
from nmea2log.fix_array import AttitudeArray
from nmea2log.model import AttitudeSample
from nmea2log.sample_cache import SampleCache, summarize_attitude
from nmea2log.tripbuilder import _motion_variation

T0 = datetime(2026, 7, 15, 9, 0, 0)


def _samples(start_minute: int, minutes: int, roll_of=lambda i: float(i % 7)) -> list:
    """One attitude sample per 10 s for ``minutes`` minutes, starting ``start_minute`` after T0."""
    return [
        AttitudeSample(T0 + timedelta(minutes=start_minute, seconds=10 * i), 0.5 * (i % 3), roll_of(i))
        for i in range(minutes * 6)
    ]


def _sample_tuple(attitude: dict) -> tuple:
    return ({}, {}, [], [], {}, {}, {}, [], attitude)


def _ebl(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"x" * 10)
    return path


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr("nmea2log.attitude_source.log", lambda message, **kwargs: lines.append(message))
    monkeypatch.setattr("nmea2log.fix_array.log", lambda message, **kwargs: lines.append(message))
    return lines


def _segments_from_cache(tmp_path: Path, files: dict, cache: SampleCache) -> AttitudeSegments:
    """Puts each file's samples in the cache and registers it the way the pipeline does on a hit."""
    segments = AttitudeSegments(cache)
    for name, by_source in files.items():
        path = _ebl(tmp_path, name)
        cache.put(path, _sample_tuple(by_source), None)
        samples, _ = cache.get(path, attitude_as_summary=True)
        segments.add_from_cache(path, samples[8])
    return segments


def test_between_reads_only_the_rows_of_the_window_from_the_cache(tmp_path: Path):
    cache = SampleCache(tmp_path / "cache")
    files = {"a.ebl": {10: _samples(0, 30)}, "b.ebl": {10: _samples(30, 30)}}
    source = _segments_from_cache(tmp_path, files, cache).dominant_source()

    window = source.between(T0 + timedelta(minutes=10), T0 + timedelta(minutes=20))

    assert list(window) == [s for s in files["a.ebl"][10] if T0 + timedelta(minutes=10) <= s.time <= T0 + timedelta(minutes=20)]


def test_between_only_opens_the_files_that_overlap_the_window(tmp_path: Path, monkeypatch):
    cache = SampleCache(tmp_path / "cache")
    files = {"a.ebl": {10: _samples(0, 30)}, "b.ebl": {10: _samples(30, 30)}, "c.ebl": {10: _samples(60, 30)}}
    source = _segments_from_cache(tmp_path, files, cache).dominant_source()
    opened = []
    real = cache.get_attitude
    monkeypatch.setattr(cache, "get_attitude", lambda path: opened.append(path.name) or real(path))

    source.between(T0 + timedelta(minutes=35), T0 + timedelta(minutes=40))

    assert opened == ["b.ebl"]


def test_a_window_spanning_two_files_joins_them_in_time_order(tmp_path: Path):
    cache = SampleCache(tmp_path / "cache")
    files = {"a.ebl": {10: _samples(0, 30)}, "b.ebl": {10: _samples(30, 30)}}
    source = _segments_from_cache(tmp_path, files, cache).dominant_source()

    window = source.between(T0 + timedelta(minutes=25), T0 + timedelta(minutes=35))

    times = [s.time for s in window]
    assert times == sorted(times)
    assert times[0] < T0 + timedelta(minutes=30) < times[-1]


def test_the_source_with_the_most_samples_over_all_files_is_used(tmp_path: Path):
    cache = SampleCache(tmp_path / "cache")
    files = {
        "a.ebl": {10: _samples(0, 30), 11: _samples(0, 10)},
        "b.ebl": {10: _samples(30, 5), 11: _samples(30, 30)},
    }
    source = _segments_from_cache(tmp_path, files, cache).dominant_source()

    assert len(source) == 10 * 6 + 30 * 6  # source 11 (240 rows) beats source 10 (210)
    window = source.between(T0, T0 + timedelta(minutes=100))
    assert list(window) == sorted(files["a.ebl"][11] + files["b.ebl"][11], key=lambda s: s.time)


def test_without_a_sample_cache_the_samples_stay_in_memory(tmp_path: Path):
    segments = AttitudeSegments(None)
    segments.add_decoded(_ebl(tmp_path, "a.ebl"), {10: _samples(0, 30)}, stored_in_cache=False)
    segments.add_decoded(_ebl(tmp_path, "b.ebl"), {10: _samples(30, 30)}, stored_in_cache=False)

    window = segments.dominant_source().between(T0 + timedelta(minutes=25), T0 + timedelta(minutes=35))

    assert len(window) == 2 * 5 * 6 + 1  # both files' rows of the window, the shared minute 30 counted once
    assert [s.time for s in window] == sorted(s.time for s in window)


def _cache_without_entries(tmp_path: Path, files: dict, redecode=None) -> AttitudeSegments:
    """Segments for ``files`` whose sample cache entries are gone again (deleted, or another version)."""
    cache = SampleCache(tmp_path / "cache")
    segments = AttitudeSegments(cache, redecode)
    for name, by_source in files.items():
        path = _ebl(tmp_path, name)
        cache.put(path, _sample_tuple(by_source), None)
        samples, _ = cache.get(path, attitude_as_summary=True)
        segments.add_from_cache(path, samples[8], {"time": name})
    for entry in (tmp_path / "cache").iterdir():
        entry.unlink()
    return segments


def test_a_file_missing_from_the_cache_is_decoded_again_with_its_own_time_state(tmp_path: Path, logged):
    files = {"a.ebl": {10: _samples(0, 30)}}
    calls = []

    def redecode(path, time_state):
        calls.append((path.name, time_state))
        return files[path.name]

    segments = _cache_without_entries(tmp_path, files, redecode)

    window = segments.dominant_source().between(T0, T0 + timedelta(minutes=30))

    assert list(window) == files["a.ebl"][10]
    assert calls == [("a.ebl", {"time": "a.ebl"})]
    assert any("Decoding a.ebl again" in line for line in logged)


def test_a_file_that_cannot_be_decoded_again_is_reported_and_left_out(tmp_path: Path, logged):
    segments = _cache_without_entries(tmp_path, {"a.ebl": {10: _samples(0, 30)}})

    window = segments.dominant_source().between(T0, T0 + timedelta(minutes=30))

    assert len(window) == 0
    assert any("[anomaly]" in line and "a.ebl" in line for line in logged)


def test_no_attitude_at_all_gives_empty_windows():
    source = AttitudeSegments(None).dominant_source()

    assert len(source) == 0
    assert len(source.between(T0, T0 + timedelta(hours=1))) == 0


def test_rows_going_back_in_time_are_reported_once_for_the_season(tmp_path: Path, logged):
    cache = SampleCache(tmp_path / "cache")
    bad = _samples(0, 10)
    bad[20], bad[21] = bad[21], bad[20]  # two rows swapped: one goes back by 10 s
    files = {"a.ebl": {10: bad}, "b.ebl": {10: _samples(10, 10)}}
    source = _segments_from_cache(tmp_path, files, cache).dominant_source()
    source.between(T0, T0 + timedelta(minutes=5))
    source.between(T0, T0 + timedelta(minutes=9))

    anomalies = [line for line in logged if "[anomaly]" in line and "Attitude samples" in line]
    assert len(anomalies) == 1
    assert "dropped 1 row(s)" in anomalies[0]


def test_a_file_starting_before_the_previous_one_ended_is_reported(tmp_path: Path, logged):
    cache = SampleCache(tmp_path / "cache")
    files = {"a.ebl": {10: _samples(0, 20)}, "b.ebl": {10: _samples(10, 20)}}

    _segments_from_cache(tmp_path, files, cache).dominant_source()

    assert any("start before the previous file ended" in line for line in logged)


def test_a_file_in_order_reports_nothing(tmp_path: Path, logged):
    cache = SampleCache(tmp_path / "cache")
    files = {"a.ebl": {10: _samples(0, 10)}, "b.ebl": {10: _samples(10, 10)}}

    _segments_from_cache(tmp_path, files, cache).dominant_source()

    assert logged == []


def test_trip_statistics_are_the_same_as_from_the_whole_season_array(tmp_path: Path):
    cache = SampleCache(tmp_path / "cache")
    files = {name: {10: _samples(30 * i, 30, roll_of=lambda k, i=i: float((k * (i + 3)) % 11) - 5)} for i, name in
             enumerate(["a.ebl", "b.ebl", "c.ebl"])}
    source = _segments_from_cache(tmp_path, files, cache).dominant_source()
    whole = AttitudeArray(s for by_source in files.values() for s in by_source[10])
    start, end = T0 + timedelta(minutes=17), T0 + timedelta(minutes=71)

    assert _motion_variation(source, start, end) == _motion_variation(whole, start, end)


def test_get_with_a_summary_leaves_every_other_part_as_it_was(tmp_path: Path):
    cache = SampleCache(tmp_path / "cache")
    path = _ebl(tmp_path, "a.ebl")
    samples = _sample_tuple({10: _samples(0, 5)})
    cache.put(path, samples, {"t": 1})

    full, _ = cache.get(path)
    summarised, state = cache.get(path, attitude_as_summary=True)

    assert summarised[:8] == full[:8]
    assert state == {"t": 1}
    assert summarised[8] == summarize_attitude({10: _samples(0, 5)})
    assert summarised[8][10].count == 30
