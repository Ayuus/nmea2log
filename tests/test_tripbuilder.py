from datetime import datetime, timedelta

import pytest

from nmea2000processor.model import DepthSample, EngineSample, PositionFix, SogSample, TripFuelSample
from nmea2000processor.tripbuilder import build_trips


class _StubGeocoder:
    def __init__(self) -> None:
        self.calls = []

    def place_name(self, lat: float, lon: float) -> str:
        self.calls.append((lat, lon))
        return f"Port@{lat:.2f},{lon:.2f}"


def _dt(minute: int) -> datetime:
    return datetime(2026, 7, 15, 8, 0, 0) + timedelta(minutes=minute)


def _build_scenario():
    """12 min stationary -> 30 min underway -> 12 min stationary, with engine data alongside."""
    fixes = []
    sogs = []

    for m in range(0, 12):
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))

    for i, m in enumerate(range(12, 42)):
        frac = i / 29
        fixes.append(PositionFix(_dt(m), 52.30 + 0.10 * frac, 4.90 + 0.05 * frac))
        sogs.append(SogSample(_dt(m), 3.0))  # ~5.8 kn, above the default threshold

    for m in range(42, 54):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))

    engine_samples = []
    for m in range(0, 54):
        fuel_rate = 8.0 if 12 <= m < 42 else 0.5
        engine_samples.append(EngineSample(_dt(m), 0, fuel_rate, 3600 * 100 + m * 60))

    return fixes, sogs, engine_samples


def test_build_trips_single_leg():
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.depart_place.startswith("Port@52.30")
    assert trip.arrive_place.startswith("Port@52.40")
    assert trip.distance_nm > 5
    assert trip.fuel_liters > 0
    assert 0 in trip.engine_hours
    assert trip.engine_hours[0] > 0
    assert trip.fuel_liters_device is None  # no PGN 127497 supplied
    assert trip.avg_speed_kn == pytest.approx(3.0 / 0.514444, rel=1e-3)
    assert trip.max_speed_kn == pytest.approx(3.0 / 0.514444, rel=1e-3)
    assert trip.min_depth_m is None  # no depth data supplied
    # engine 0 does have samples, but none of the health fields were populated
    assert trip.engine_health[0].oil_pressure_bar_avg is None
    assert trip.engine_health[0].warnings == frozenset()
    # the geocoder must not be called more often than the number of port visits
    assert len(geocoder.calls) == 2


def test_trip_fuel_device_delta():
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    # engine's own trip meter: rises steadily, only relevant to the trip while underway
    trip_fuel_samples = [
        TripFuelSample(_dt(m), 0, 100.0 + m * 0.2) for m in range(0, 54)
    ]

    trips = build_trips(
        fixes, sogs, engine_samples, trip_fuel_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.fuel_liters_device is not None
    assert trip.fuel_liters_device > 0
    # difference between the trip's start and end reading, not the whole period
    assert trip.fuel_liters_device < (100.0 + 53 * 0.2) - 100.0


def test_min_depth_with_position():
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    # depth varies while underway (m 12..41), with a clear low point at m=25
    depth_samples = []
    for m in range(0, 54):
        if 12 <= m < 42:
            depth = 2.0 if m == 25 else 8.0
        else:
            depth = 15.0  # in the port, not relevant to this trip
        depth_samples.append(DepthSample(_dt(m), depth))

    trips = build_trips(
        fixes, sogs, engine_samples, None, depth_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.min_depth_m == pytest.approx(2.0)
    # the shallowest position belongs to fix m=25, somewhere on the straight line between the ports
    assert trip.min_depth_lat is not None
    assert trip.min_depth_lon is not None


def test_engine_health_and_warnings():
    fixes, sogs, _unused_engine = _build_scenario()
    geocoder = _StubGeocoder()

    engine_samples = []
    for m in range(0, 54):
        fuel_rate = 8.0 if 12 <= m < 42 else 0.5
        warnings = frozenset({"Low Oil Pressure"}) if m == 20 else frozenset()
        engine_samples.append(
            EngineSample(
                _dt(m), 0, fuel_rate, 3600 * 100 + m * 60,
                oil_pressure_pa=300000.0,
                oil_temperature_k=350.0,
                coolant_temperature_k=355.0,
                alternator_voltage_v=14.2,
                engine_load_pct=60.0 if m != 30 else 90.0,
                warnings=warnings,
            )
        )

    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 1
    health = trips[0].engine_health[0]
    assert health.oil_pressure_bar_avg == pytest.approx(3.0)
    assert health.coolant_temperature_c_avg == pytest.approx(355.0 - 273.15)
    assert health.alternator_voltage_v_avg == pytest.approx(14.2)
    assert health.engine_load_pct_max == pytest.approx(90.0)
    # the warning active at m=20 (while underway) must be visible
    assert "Low Oil Pressure" in health.warnings


def test_short_stop_does_not_split_trip():
    """A short stop (< min_stop_minutes) shouldn't count as a separate port visit."""
    fixes, sogs, engine_samples = _build_scenario()

    # add a short 3-minute stop halfway through the trip
    extra_fixes = [PositionFix(_dt(25) + timedelta(seconds=s), 52.35, 4.925) for s in range(0, 180, 20)]
    extra_sogs = [SogSample(_dt(25) + timedelta(seconds=s), 0.0) for s in range(0, 180, 20)]

    fixes = fixes[:39] + extra_fixes + fixes[39:]
    sogs = sogs[:39] + extra_sogs + sogs[39:]

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 1


def test_no_samples_returns_no_trips():
    assert build_trips([], [], []) == []


def test_large_data_gap_keeps_harbor_but_excludes_gap_from_duration():
    """Regression test for a real bug, found with real data: a large gap in the data (e.g. the
    device/log was down for a while) let the reported duration run on well past the gap (in
    practice: 3:21 instead of the real ~0:45, while the engine-hours counter -- which doesn't
    depend on GPS classification -- had it right).

    An earlier fix solved that by cutting trips into separate pieces at every gap, but that in
    turn broke the port linkage: a stationary period of only a few minutes right before the gap
    (too short for min_stop_minutes) didn't count as a port visit, so the arrival port of the
    trip before it became "Unknown" instead of the real (confirmed) mooring.

    The right approach: such a short stationary period still counts as a port visit if it's
    adjacent to the gap (_merge_short_stops), but the *reported duration* of the trip
    (``trip.duration``) ignores the gap (_moving_duration) -- so the port stays linked, the
    duration isn't artificially stretched."""
    fixes, sogs, engine_samples = _build_scenario()  # 12 min stationary -> 30 min underway -> 12 min stationary

    # after the trip: a stationary period of only 5 minutes (too short for min_stop_minutes=10)
    short_stop_start = 54
    for m in range(short_stop_start, short_stop_start + 5):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    # then a 3-hour gap with no data at all (device/log was down)
    gap_end_minute = short_stop_start + 5 + 180

    # after the gap: a separate, short trip (well above min_trip_distance_nm)
    for i, m in enumerate(range(gap_end_minute, gap_end_minute + 3)):
        frac = i / 2
        fixes.append(PositionFix(_dt(m), 52.40 + 0.01 * frac, 4.95 + 0.01 * frac))
        sogs.append(SogSample(_dt(m), 2.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.0, 3600 * 100 + m * 60))  # engine off

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 2  # the main trip, and the short trip after the gap, separately
    first_trip, second_trip = trips

    # the main trip keeps its real arrival port (not "Unknown"!)...
    assert first_trip.arrive_place == "Port@52.40,4.95"
    # ...with a duration that doesn't bridge the gap
    assert first_trip.duration < timedelta(hours=1)

    # the trip after the gap logically departs from that same port
    assert second_trip.depart_place == "Port@52.40,4.95"
    assert second_trip.arrive_place == "Unknown (end outside log file)"


def test_negligible_distance_trip_is_filtered_out():
    """Trips with negligible distance (GPS/speed noise, often right at a segment boundary)
    shouldn't show up as a logbook row."""
    fixes, sogs, engine_samples = _build_scenario()

    # port visit (>= min_stop_minutes), followed by a tiny "trip" of a few meters
    for m in range(54, 54 + 12):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.0, 3600 * 100 + m * 60))
    for m in range(66, 66 + 2):
        fixes.append(PositionFix(_dt(m), 52.4001, 4.9501))  # a few meters further on
        sogs.append(SogSample(_dt(m), 2.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.0, 3600 * 100 + m * 60))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    # only the original trip (52.30 -> 52.40) should remain
    assert len(trips) == 1
    assert trips[0].arrive_place == "Port@52.40,4.95"


def test_min_trip_distance_nm_can_be_disabled():
    fixes, sogs, engine_samples = _build_scenario()
    for m in range(54, 54 + 12):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.0, 3600 * 100 + m * 60))
    for m in range(66, 66 + 2):
        fixes.append(PositionFix(_dt(m), 52.4001, 4.9501))
        sogs.append(SogSample(_dt(m), 2.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.0, 3600 * 100 + m * 60))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10, min_trip_distance_nm=0.0,
    )

    assert len(trips) == 2  # with the filter disabled, the tiny trip counts too


def test_gap_masked_by_moving_noise_on_both_sides_still_splits_the_trip():
    """Regression test for a real bug found with real data: right as the boat actually arrived
    and stopped, a couple of noisy SOG readings stayed just above speed_threshold_kn (GPS jitter
    while moored), and the same happened again once data resumed after a long gap -- so the
    whole gap was labelled "moving" on both sides, with no differently-labelled sample for
    _classify_runs to split on. Without a fix, the arrival was never recognized as a stay, the
    reported duration bridged the gap, and the CSV showed the *next* real stay (found hours
    later) as the arrival port instead of the true one."""
    fixes = []
    sogs = []
    engine_samples = []

    # haven A: 12 minutes stationary
    for m in range(0, 12):
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    # real trip: 13 minutes underway, arriving at haven B
    for i, m in enumerate(range(12, 25)):
        frac = i / 12
        fixes.append(PositionFix(_dt(m), 52.30 + 0.05 * frac, 4.90 + 0.05 * frac))
        sogs.append(SogSample(_dt(m), 3.0))
        engine_samples.append(EngineSample(_dt(m), 0, 8.0, 3600 * 100 + m * 60))

    # GPS noise right at arrival (m=25) and right after a long gap (m=40, m=41): SOG stays just
    # above the threshold even though the boat is actually already moored at haven B
    for m in (25, 40, 41):
        fixes.append(PositionFix(_dt(m), 52.35, 4.95))
        sogs.append(SogSample(_dt(m), 0.3))  # just above speed_threshold_kn=0.25 kn used below
        engine_samples.append(EngineSample(_dt(m), 0, 0.0, 3600 * 100 + m * 60))

    # haven B: confirmed long stop from m=42
    for m in range(42, 54):
        fixes.append(PositionFix(_dt(m), 52.35, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.0, 3600 * 100 + m * 60))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples,
        geocoder=geocoder, speed_threshold_kn=0.25, min_stop_minutes=10, max_gap_minutes=10,
    )

    assert len(trips) == 1  # the phantom 0 nm hop across the gap is filtered out
    trip = trips[0]
    assert trip.depart_place == "Port@52.30,4.90"
    assert trip.arrive_place == "Port@52.35,4.95"  # not "Unknown" and not haven B found hours later
    assert trip.duration < timedelta(minutes=20)  # doesn't bridge the ~15 minute gap


def test_small_data_gap_does_not_split_trip():
    """A small gap (e.g. a few seconds between two log files) shouldn't unnecessarily split a
    trip -- only gaps of at least max_gap_minutes do that."""
    fixes, sogs, engine_samples = _build_scenario()

    # 2-minute gap (43 -> 45) in the middle of the underway period, well under max_gap_minutes (10)
    fixes = [f for f in fixes if not (43 <= (f.time - _dt(0)).total_seconds() / 60 < 45)]
    sogs = [s for s in sogs if not (43 <= (s.time - _dt(0)).total_seconds() / 60 < 45)]

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 1  # unchanged behavior: the small gap is ignored
