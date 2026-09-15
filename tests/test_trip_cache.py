from datetime import datetime
from pathlib import Path

from nmea2log.trip_cache import (
    TripCache,
    choose_resume_index,
    config_signature,
    find_resume_index,
)


def test_config_signature_is_stable_for_the_same_parameters():
    assert config_signature(a=1, b="x") == config_signature(b="x", a=1)  # order-independent


def test_config_signature_changes_when_a_parameter_changes():
    assert config_signature(speed_threshold_kn=0.5) != config_signature(speed_threshold_kn=0.6)


def test_find_resume_index_matches_by_resolved_path(tmp_path):
    f0 = tmp_path / "f0.ebl"
    f1 = tmp_path / "f1.ebl"
    f0.write_bytes(b"x")
    f1.write_bytes(b"x")

    assert find_resume_index([f0, f1], str(f1.resolve())) == 1


def test_find_resume_index_returns_none_when_the_file_is_gone(tmp_path):
    f0 = tmp_path / "f0.ebl"
    f0.write_bytes(b"x")

    assert find_resume_index([f0], str((tmp_path / "missing.ebl").resolve())) is None


def test_choose_resume_index_picks_the_last_file_starting_at_or_before_the_reference_time():
    """The chosen file is the file the reference time (the start of the stay right before the
    still-open last trip's own departure) itself falls in -- not one earlier, which would still
    contain moving-leg data belonging to the already-cached previous trip and reintroduce it a
    second time (found in practice, see choose_resume_index()'s own docstring)."""
    reference_time = datetime(2026, 7, 15, 10, 0)
    file_first_times = [
        (0, datetime(2026, 7, 15, 8, 0)),
        (1, datetime(2026, 7, 15, 9, 0)),
        (2, datetime(2026, 7, 15, 10, 0)),  # the file the reference time itself falls in
        (3, datetime(2026, 7, 15, 11, 0)),  # after the reference time -- must not be picked
    ]

    assert choose_resume_index(file_first_times, reference_time, min_index=0) == 2


def test_choose_resume_index_never_goes_earlier_than_min_index():
    """Must never point at a file earlier than what this run itself had available to decode --
    worst case it just resumes from the same place as before, which is still correct, only less
    of a saving."""
    reference_time = datetime(2026, 7, 15, 6, 0)  # earlier than every known file
    file_first_times = [(2, datetime(2026, 7, 15, 10, 0))]

    assert choose_resume_index(file_first_times, reference_time, min_index=2) == 2


def test_choose_resume_index_falls_back_to_min_index_when_nothing_matches():
    """None of the known files start early enough (e.g. the trip cache was just seeded and the
    very first file already starts after the departure being looked up) -- must not crash or
    return a nonsensical negative/None index."""
    depart_time = datetime(2026, 7, 15, 6, 0)  # before every known file
    file_first_times = [(0, datetime(2026, 7, 15, 8, 0))]

    assert choose_resume_index(file_first_times, depart_time, min_index=0) == 0


def _fake_trip():
    """A minimal stand-in with just enough shape to round-trip through pickle -- the real
    TripLeg dataclass is exercised by the cli.py integration tests; this module only needs
    something picklable to prove TripCache itself works."""
    return {"depart_time": datetime(2026, 7, 15, 8, 0)}


def test_trip_cache_round_trips_through_save_and_load(tmp_path):
    cache = TripCache(tmp_path / "trips.pkl")
    trips = [_fake_trip()]

    cache.save(trips, "resume.ebl", "some-state", "sig-1")
    loaded = cache.load("sig-1")

    assert loaded == (trips, "resume.ebl", "some-state")


def test_trip_cache_load_returns_none_when_the_file_does_not_exist(tmp_path):
    cache = TripCache(tmp_path / "missing.pkl")

    assert cache.load("sig-1") is None


def test_trip_cache_load_returns_none_on_a_config_signature_mismatch(tmp_path):
    """A cache built under different build_trips() parameters (e.g. a different
    --speed-threshold-kn) must never be silently trusted -- see config_signature()."""
    cache = TripCache(tmp_path / "trips.pkl")
    cache.save([_fake_trip()], "resume.ebl", None, "sig-old")

    assert cache.load("sig-new") is None


def test_trip_cache_load_returns_none_on_a_corrupt_file(tmp_path):
    path = tmp_path / "trips.pkl"
    path.write_bytes(b"not a valid cache")
    cache = TripCache(path)

    assert cache.load("sig-1") is None


def test_trip_cache_clear_removes_the_file(tmp_path):
    path = tmp_path / "trips.pkl"
    cache = TripCache(path)
    cache.save([_fake_trip()], "resume.ebl", None, "sig-1")
    assert path.exists()

    cache.clear()

    assert not path.exists()
    cache.clear()  # must not raise when already gone
