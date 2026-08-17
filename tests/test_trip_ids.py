from datetime import datetime, timedelta

from nmea2000processor.trip_ids import assign_trip_ids
from nmea2000processor.tripbuilder import NavSample, TripLeg


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


def test_same_trip_gets_the_same_uid_across_runs():
    """The id is a pure function of the trip's own match key (see trip_ids.py's module
    docstring for why this replaced a random uuid4 + persisted registry file) -- no shared state
    of any kind needed for two separate calls to agree, which also means two runs against the
    project directory at the same time can never race or clobber each other's ids."""
    trip = _trip()

    first = assign_trip_ids([trip])
    second = assign_trip_ids([_trip()])  # a fresh, equal-but-distinct object

    assert first == second
    assert len(first[0]) > 0


def test_uid_survives_a_shift_in_exact_departure_time():
    """The whole point of the id: a future trip-recognition fix shifting a trip's exact
    depart/arrive time by a few minutes must not orphan it -- matching is on local date +
    position, not the exact timestamp."""
    original = _trip(depart_time=datetime(2026, 7, 15, 9, 0), arrive_time=datetime(2026, 7, 15, 10, 30))
    shifted = _trip(depart_time=datetime(2026, 7, 15, 9, 12), arrive_time=datetime(2026, 7, 15, 10, 18))

    first = assign_trip_ids([original])
    second = assign_trip_ids([shifted])

    assert first == second


def test_different_days_get_different_uids():
    day1 = _trip(depart_time=datetime(2026, 7, 15, 9, 0))
    day2 = _trip(depart_time=datetime(2026, 7, 16, 9, 0))

    uids = assign_trip_ids([day1, day2])

    assert uids[0] != uids[1]


def test_repeated_same_day_same_route_trips_get_distinct_uids_via_occurrence():
    trips = [_trip(), _trip()]  # two identical round trips logged the same day

    first_run = assign_trip_ids(trips)
    assert first_run[0] != first_run[1]

    # Re-running with the same two trips (fresh objects) must reuse both, in order.
    second_run = assign_trip_ids([_trip(), _trip()])
    assert second_run == first_run


def test_uid_survives_a_changed_place_name_at_the_same_position():
    """Regression test for a real bug: matching used to be on depart_place/arrive_place *text*,
    which broke every time that text changed for a reason unrelated to the trip itself --
    geocoding on vs. off, a future geocoding fix returning a different name for the same spot,
    --language -- silently orphaning the trip's uid and, with it, any remark already saved
    against the old one (found in practice: remarks disappearing after every upload, because
    some uploads used --no-geocode and some didn't). Matching a track's own GPS position instead
    of the displayed name must survive exactly this."""
    track = [
        NavSample(datetime(2026, 7, 15, 9, 0), 52.30000, 4.90000, 0.0),
        NavSample(datetime(2026, 7, 15, 10, 30), 52.35000, 4.95000, 0.0),
    ]
    no_geocode_run = _trip(depart_place="52.3000, 4.9000", arrive_place="52.3500, 4.9500", track=track)
    geocoded_run = _trip(depart_place="Marina A", arrive_place="Marina B", track=track)

    first = assign_trip_ids([no_geocode_run])
    second = assign_trip_ids([geocoded_run])

    assert first == second
