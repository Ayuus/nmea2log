from datetime import datetime, timedelta
from pathlib import Path

from nmea2000processor.trip_ids import assign_trip_ids
from nmea2000processor.tripbuilder import TripLeg


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


def test_same_trip_gets_the_same_uid_across_runs(tmp_path: Path):
    registry_path = tmp_path / "trip_ids.json"
    trip = _trip()

    first = assign_trip_ids([trip], registry_path)
    second = assign_trip_ids([_trip()], registry_path)  # a fresh, equal-but-distinct object

    assert first == second
    assert len(first[0]) > 0


def test_uid_survives_a_shift_in_exact_departure_time(tmp_path: Path):
    """The whole point of the uid: a future trip-recognition fix shifting a trip's exact
    depart/arrive time by a few minutes must not orphan its id -- matching is on local date +
    ports, not the exact timestamp."""
    registry_path = tmp_path / "trip_ids.json"
    original = _trip(depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 30))
    shifted = _trip(depart_time=datetime(2026, 7, 15, 9, 12), arrive_time=datetime(2026, 7, 15, 10, 18))

    first = assign_trip_ids([original], registry_path)
    second = assign_trip_ids([shifted], registry_path)

    assert first == second


def test_different_days_get_different_uids(tmp_path: Path):
    registry_path = tmp_path / "trip_ids.json"
    day1 = _trip(depart_time=datetime(2026, 7, 15, 9, 0))
    day2 = _trip(depart_time=datetime(2026, 7, 16, 9, 0))

    uids = assign_trip_ids([day1, day2], registry_path)

    assert uids[0] != uids[1]


def test_repeated_same_day_same_route_trips_get_distinct_uids_via_occurrence(tmp_path: Path):
    registry_path = tmp_path / "trip_ids.json"
    trips = [_trip(), _trip()]  # two identical round trips logged the same day

    first_run = assign_trip_ids(trips, registry_path)
    assert first_run[0] != first_run[1]

    # Re-running with the same two trips (fresh objects) must reuse both, in order.
    second_run = assign_trip_ids([_trip(), _trip()], registry_path)
    assert second_run == first_run


def test_registry_file_is_created_and_reused(tmp_path: Path):
    registry_path = tmp_path / "trip_ids.json"
    assert not registry_path.exists()

    assign_trip_ids([_trip()], registry_path)

    assert registry_path.exists()
