from datetime import datetime, timedelta

import pytest

from nmea2log.model import (
    AttitudeSample,
    BatterySample,
    DepthSample,
    EngineRpmSample,
    EngineSample,
    PositionFix,
    SogSample,
    TripFuelSample,
    WaterTempSample,
)
from nmea2log.fix_array import FixArray
from nmea2log.tripbuilder import _reject_gps_outliers, _reject_gps_outliers_array, build_trips


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
    # absolute hour-meter reading at arrival, not just the delta run during this trip -- the
    # scenario's engine samples start at total_hours_s = 3600*100 (i.e. 100h already on the clock)
    assert trip.engine_hours_total[0] == pytest.approx(100.7, abs=0.05)
    assert trip.fuel_liters_device is None  # no PGN 127497 supplied
    assert trip.avg_speed_kn == pytest.approx(3.0 / 0.514444, rel=1e-3)
    assert trip.max_speed_kn == pytest.approx(3.0 / 0.514444, rel=1e-3)
    assert trip.min_depth_m is None  # no depth data supplied
    # engine 0 does have samples, but none of the health fields were populated
    assert trip.engine_health[0].oil_pressure_bar_avg is None
    assert trip.engine_health[0].warnings == frozenset()
    # the geocoder must not be called more often than the number of port visits
    assert len(geocoder.calls) == 2


def test_trip_depart_and_arrive_lat_lon_are_the_stays_averaged_position_not_a_single_fix():
    """Regression test: depart_lat/lon and arrive_lat/lon used to come from the trip's own first/
    last GPS fix (the exact moment the boat started/stopped moving) -- a single fix, with real GPS
    jitter (found in practice: ~10 m off from the actual berth). The stay itself already averages
    every stationary fix during the port visit for its own place-name lookup (see Stay in
    tripbuilder.py); depart/arrive_lat/lon must use that same averaged position, not reintroduce
    the single-fix jitter for anything that shows the position on a map."""
    fixes = []
    sogs = []
    # stationary, but not at one exact point -- small jitter around 52.30/4.90, alternating so the
    # mean differs from every individual fix (found in practice: consumer GPS jitter even at rest)
    for m in range(0, 12):
        jitter = 0.0002 if m % 2 == 0 else -0.0002
        fixes.append(PositionFix(_dt(m), 52.30 + jitter, 4.90 + jitter))
        sogs.append(SogSample(_dt(m), 0.0))
    for i, m in enumerate(range(12, 42)):
        frac = i / 29
        fixes.append(PositionFix(_dt(m), 52.30 + 0.10 * frac, 4.90 + 0.05 * frac))
        sogs.append(SogSample(_dt(m), 3.0))
    for m in range(42, 54):
        jitter = 0.0003 if m % 2 == 0 else -0.0003
        fixes.append(PositionFix(_dt(m), 52.40 + jitter, 4.95 + jitter))
        sogs.append(SogSample(_dt(m), 0.0))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, [], geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 1
    trip = trips[0]
    # the mean of an equal number of +jitter/-jitter fixes is the unjittered centre point --
    # nowhere near any single fix actually recorded (each was 0.0002/0.0003 deg off-centre)
    assert trip.depart_lat == pytest.approx(52.30, abs=1e-9)
    assert trip.depart_lon == pytest.approx(4.90, abs=1e-9)
    assert trip.arrive_lat == pytest.approx(52.40, abs=1e-9)
    assert trip.arrive_lon == pytest.approx(4.95, abs=1e-9)


def test_arrival_position_excludes_still_gliding_samples_right_after_arrival():
    """Regression test for a real bug found with real data: a boat gliding the last few metres
    into a berth crosses below speed_threshold_kn (already counted "stationary" by
    _classify_runs) before it's actually stopped moving -- a real arrival's last "moving" sample
    was recorded doing exactly the default 0.5 kn threshold, meaning the stay's own first couple
    of samples right after are still likely doing a bit less than that while continuing to glide
    those last few metres, not yet at the boat's true final position. Averaging the whole stay
    from its very first sample (as if it were already fully at rest) pulled the reported arrival
    a little way back along the approach path -- small in absolute terms (a few metres) but a
    real, visible gap between the drawn track's own last point and the arrival marker on a
    zoomed-in map. Only samples under half the classification threshold now count towards the
    stay's own averaged position (see _settled_position)."""
    fixes = []
    sogs = []

    for m in range(0, 12):
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))
    for i, m in enumerate(range(12, 40)):
        frac = i / 27
        fixes.append(PositionFix(_dt(m), 52.30 + 0.10 * frac, 4.90))
        sogs.append(SogSample(_dt(m), 3.0))  # ~5.8 kn, underway
    # still gliding into the berth: already classified "stationary" (0.20/0.15 m/s < the 0.5 kn
    # / 0.2572 m/s threshold) but not yet settled (still above half that, 0.1286 m/s) -- position
    # still moving towards the boat's real final spot
    fixes.append(PositionFix(_dt(40), 52.4010, 4.90))
    sogs.append(SogSample(_dt(40), 0.20))
    fixes.append(PositionFix(_dt(41), 52.4005, 4.90))
    sogs.append(SogSample(_dt(41), 0.15))
    # genuinely settled from here -- 12 minutes at the boat's real final position
    for m in range(42, 54):
        fixes.append(PositionFix(_dt(m), 52.40, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, [], geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    # not pytest.approx(52.40, abs=1e-9) -- the pre-fix average (all 14 samples, including the
    # two still-gliding ones) works out to roughly 52.40011, so a tolerance has to be tight
    # enough to actually catch that regression rather than accepting either result.
    assert trips[0].arrive_lat == pytest.approx(52.40, abs=1e-6)


def test_arrival_position_excludes_samples_while_the_engine_is_still_running():
    """Asked for explicitly: SOG alone can already read ~0 while the skipper is still actively
    working the boat into its berth (a bow thruster nudge, a brief burst in reverse, ...) with
    the engine still running -- _settled_position's speed-only filter can't see that. A sample
    still under power, even at zero net movement, doesn't yet count as the boat's real final
    position; only once the engine is confirmed off does a sample count towards the average."""
    fixes = []
    sogs = []
    engine_samples = []

    def add(m, lat, sog, fuel):
        fixes.append(PositionFix(_dt(m), lat, 4.90))
        sogs.append(SogSample(_dt(m), sog))
        engine_samples.append(EngineSample(_dt(m), 0, fuel, 3600 * 100 + m * 60))

    for m in range(0, 12):
        add(m, 52.30, 0.0, 0.5)  # port A, engine idling but off per _ENGINE_IDLE_FUEL_LPH
    for i, m in enumerate(range(12, 40)):
        add(m, 52.30 + 0.10 * (i / 27), 3.0, 8.0)  # underway, ~5.8 kn, engine on
    # SOG already reads 0.0 (well under the speed filter's own cutoff) but the engine is still
    # confirmed running -- still working the boat into its berth, not yet genuinely at rest
    add(40, 52.4010, 0.0, 3.0)
    add(41, 52.4005, 0.0, 3.0)
    # engine off from here -- 12 minutes at the boat's real final position
    for m in range(42, 54):
        add(m, 52.40, 0.0, 0.0)

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    assert trips[0].arrive_lat == pytest.approx(52.40, abs=1e-6)


def test_reject_gps_outliers_drops_a_single_corrupted_fix():
    """Regression test for a real incident, values taken from the actual corrupted record found
    in a real .ebl file: the correct 8-byte position payload (46.916294, -2.3801566) with its
    first 2 bytes moved to the end decodes to (-78.629377, -60.8371052), ~7800 nm away -- for one
    sample, a fraction of a second after the correct reading. The fix right after the bad one must
    survive too -- it's the bad fix that's the outlier, not the ones around it."""
    good_before = PositionFix(datetime(2026, 8, 25, 8, 13, 55), 46.916294, -2.3801566)
    bad = PositionFix(datetime(2026, 8, 25, 8, 13, 56), -78.629377, -60.8371052)
    good_after = PositionFix(datetime(2026, 8, 25, 8, 13, 56), 46.9162885, -2.3801523)

    kept = _reject_gps_outliers([good_before, bad, good_after])

    assert kept == [good_before, good_after]


def test_reject_gps_outliers_array_drops_a_single_corrupted_fix():
    """Same real-world scenario as test_reject_gps_outliers_drops_a_single_corrupted_fix() above,
    against the FixArray-native version _merge_nav_samples() actually calls (see fix_array.py --
    both versions exist so the season-wide, memory-conscious path gets the exact same real-bug
    coverage as the plain-list one)."""
    good_before = PositionFix(datetime(2026, 8, 25, 8, 13, 55), 46.916294, -2.3801566)
    bad = PositionFix(datetime(2026, 8, 25, 8, 13, 56), -78.629377, -60.8371052)
    good_after = PositionFix(datetime(2026, 8, 25, 8, 13, 56), 46.9162885, -2.3801523)

    kept = _reject_gps_outliers_array(FixArray([good_before, bad, good_after]))

    assert kept == [good_before, good_after]


def test_build_trips_ignores_a_single_gps_glitch_in_distance():
    fixes, sogs, engine_samples = _build_scenario()
    # a single corrupted fix landing mid-trip, far from anywhere near the real track
    fixes.insert(20, PositionFix(_dt(20) + timedelta(seconds=1), -78.6, -60.8))
    geocoder = _StubGeocoder()

    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 1
    # the real trip covers a fraction of a degree (~6 nm); thousands of nm would mean the glitch
    # leaked through
    assert trips[0].distance_nm < 20


def test_max_speed_records_when_and_at_what_rpm_it_happened():
    """The bare max speed number doesn't say whether it was a brief downwind surge at low RPM or
    genuinely flat-out -- max_speed_at/max_speed_rpm let a caller show that context (e.g. in a
    tooltip) instead of just the number on its own."""
    fixes = []
    sogs = []
    engine_samples = []
    rpm_samples = []

    for m in range(0, 12):
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    for i, m in enumerate(range(12, 42)):
        frac = i / 29
        fixes.append(PositionFix(_dt(m), 52.30 + 0.10 * frac, 4.90 + 0.05 * frac))
        if m == 25:
            sogs.append(SogSample(_dt(m), 9.0))  # brief spike, ~17.5 kn
            rpm_samples.append(EngineRpmSample(_dt(m), 0, 3400.0))
        else:
            sogs.append(SogSample(_dt(m), 6.5))  # steady cruise, ~12.6 kn
            rpm_samples.append(EngineRpmSample(_dt(m), 0, 2200.0))
        engine_samples.append(EngineSample(_dt(m), 0, 8.0, 3600 * 100 + m * 60))

    for m in range(42, 54):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, rpm_samples=rpm_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.max_speed_kn == pytest.approx(9.0 / 0.514444, rel=1e-3)
    assert trip.max_speed_at == _dt(25)
    assert trip.max_speed_rpm[0] == pytest.approx(3400.0)


def test_track_carries_cog_forward_filled_from_sog_samples():
    """PGN 129026 reports COG and SOG together, so cog_deg is decoded and forward-filled onto
    the track exactly like sog_ms already was -- needed for the periodic log table in the HTML
    logbook (course, speed, position)."""
    fixes, sogs, engine_samples = _build_scenario()
    # give the underway samples a real course; leave the rest at the SogSample default (None)
    sogs = [
        SogSample(s.time, s.sog_ms, 225.0) if 12 <= i < 42 else s for i, s in enumerate(sogs)
    ]
    geocoder = _StubGeocoder()

    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    trip = trips[0]
    assert trip.track  # sanity: this scenario does produce track points
    underway_points = [s for s in trip.track if s.sog_ms > 0]
    assert underway_points
    assert all(s.cog_deg == pytest.approx(225.0) for s in underway_points)


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


def test_water_temp_stats_with_position():
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    # water temp varies while underway (m 12..41), rising steadily
    water_temp_samples = []
    for m in range(0, 54):
        if 12 <= m < 42:
            temp_c = 18.0 + (m - 12) * 0.1
        else:
            temp_c = 12.0  # in the port, not relevant to this trip
        water_temp_samples.append(WaterTempSample(_dt(m), temp_c))

    trips = build_trips(
        fixes, sogs, engine_samples, None, None, water_temp_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.min_water_temp_c == pytest.approx(18.0)
    assert trip.max_water_temp_c == pytest.approx(18.0 + 29 * 0.1)
    assert trip.avg_water_temp_c == pytest.approx((trip.min_water_temp_c + trip.max_water_temp_c) / 2, abs=0.2)


def test_water_temp_stats_absent_without_samples():
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    trips = build_trips(
        fixes, sogs, engine_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.avg_water_temp_c is None
    assert trip.min_water_temp_c is None
    assert trip.max_water_temp_c is None


def test_battery_health_with_low_voltage():
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    # voltage sags to a low point mid-trip, then recovers
    battery_samples = []
    for m in range(0, 54):
        if 12 <= m < 42:
            voltage = 11.5 if m == 25 else 12.8
        else:
            voltage = 12.9  # in port, not relevant to this trip
        battery_samples.append(BatterySample(_dt(m), 0, voltage))

    trips = build_trips(
        fixes, sogs, engine_samples, battery_samples=battery_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.battery_health[0].min_voltage_v == pytest.approx(11.5)
    assert trip.battery_health[0].avg_voltage_v is not None
    assert trip.battery_health[0].min_voltage_at == _dt(25)


def test_battery_health_absent_without_samples():
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    trips = build_trips(
        fixes, sogs, engine_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    assert trips[0].battery_health == {}


def test_typical_rpm_is_the_most_common_bucketed_value():
    """The typical RPM is the mode of the (bucketed) readings, not the average or maximum -- so
    it reflects the steady cruising speed rather than being skewed by idle/neutral periods or
    brief revs."""
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    rpm_samples = []
    for m in range(0, 54):
        if 12 <= m < 42:
            # mostly steady cruising rpm, with a couple of brief revs up while maneuvering
            rpm = 3200.0 if m in (13, 14) else 2200.0
        else:
            rpm = 800.0  # idling in port, not relevant to this trip
        rpm_samples.append(EngineRpmSample(_dt(m), 0, rpm))

    trips = build_trips(
        fixes, sogs, engine_samples, rpm_samples=rpm_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    assert trips[0].typical_rpm[0] == pytest.approx(2200.0)


def test_typical_rpm_absent_without_samples():
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    trips = build_trips(
        fixes, sogs, engine_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    assert trips[0].typical_rpm == {}
    assert trips[0].typical_rpm_speed_kn == {}


def test_typical_rpm_speed_range_reflects_speed_at_that_rpm_not_trip_average():
    """Regression test for a real report: the trip's overall average speed is diluted by any
    moment the boat's RPM (and thus speed) dipped away from the steady cruising value -- e.g. one
    brief coast in neutral -- so showing that average next to the typical RPM misleadingly reads
    as "this RPM only makes that speed". The speed range must reflect only the moments actually
    spent at the typical RPM."""
    fixes = []
    sogs = []
    engine_samples = []
    rpm_samples = []

    for m in range(0, 12):
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    for i, m in enumerate(range(12, 42)):
        frac = i / 29
        fixes.append(PositionFix(_dt(m), 52.30 + 0.10 * frac, 4.90 + 0.05 * frac))
        if m == 20:
            sogs.append(SogSample(_dt(m), 0.5))  # brief coast in neutral
            rpm_samples.append(EngineRpmSample(_dt(m), 0, 800.0))
        else:
            sogs.append(SogSample(_dt(m), 6.5))  # steady cruise, ~12.6 kn
            rpm_samples.append(EngineRpmSample(_dt(m), 0, 2200.0))
        engine_samples.append(EngineSample(_dt(m), 0, 8.0, 3600 * 100 + m * 60))

    for m in range(42, 54):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, rpm_samples=rpm_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.typical_rpm[0] == pytest.approx(2200.0)
    min_kn, max_kn, avg_kn, avg_fuel_l_per_nm = trip.typical_rpm_speed_kn[0]
    assert min_kn > 5.0  # the one neutral-coast sample (0.5 m/s / ~1 kn) must not drag it down
    assert max_kn == pytest.approx(6.5 / 0.514444, rel=1e-3)
    # 8.0 L/h at the steady-cruise speed of this window, expressed as L/nm
    assert avg_fuel_l_per_nm == pytest.approx(8.0 / avg_kn, rel=1e-3)


def test_typical_rpm_speed_range_ignores_a_brief_pass_through_while_accelerating():
    """A short (< 2 min) isolated run at the typical RPM bucket -- e.g. briefly passing through
    it on the way up to cruising speed -- must not count, even though the RPM value matches;
    only a *sustained* run at that bucket represents actually holding that RPM."""
    fixes = []
    sogs = []
    engine_samples = []
    rpm_samples = []

    for m in range(0, 12):
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    for i, m in enumerate(range(12, 42)):
        frac = i / 29
        fixes.append(PositionFix(_dt(m), 52.30 + 0.10 * frac, 4.90 + 0.05 * frac))
        if m in (12, 13):
            sogs.append(SogSample(_dt(m), 1.5))  # brief overshoot to 2200 rpm while accelerating
            rpm_samples.append(EngineRpmSample(_dt(m), 0, 2200.0))
        elif m == 14:
            sogs.append(SogSample(_dt(m), 4.0))  # dips back down before settling into cruise
            rpm_samples.append(EngineRpmSample(_dt(m), 0, 1600.0))
        else:
            sogs.append(SogSample(_dt(m), 6.5))  # steady cruise, ~12.6 kn
            rpm_samples.append(EngineRpmSample(_dt(m), 0, 2200.0))
        engine_samples.append(EngineSample(_dt(m), 0, 8.0, 3600 * 100 + m * 60))

    for m in range(42, 54):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, rpm_samples=rpm_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.typical_rpm[0] == pytest.approx(2200.0)
    min_kn, max_kn, avg_kn, avg_fuel_l_per_nm = trip.typical_rpm_speed_kn[0]
    # the brief 1-minute overshoot (1.5 m/s / ~2.9 kn) must be excluded -- too short to be a
    # sustained run -- leaving only the steady-cruise speed
    assert min_kn > 5.0
    assert max_kn == pytest.approx(6.5 / 0.514444, rel=1e-3)
    assert avg_fuel_l_per_nm == pytest.approx(8.0 / avg_kn, rel=1e-3)


def test_motion_variation_reflects_roll_and_pitch_spread():
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    attitude_samples = []
    for m in range(0, 54):
        if 12 <= m < 42:
            # oscillating roll/pitch while underway (choppy conditions)
            roll = 8.0 if m % 2 == 0 else -8.0
            pitch = 3.0 if m % 2 == 0 else -3.0
        else:
            roll, pitch = 0.0, 0.0  # dead calm in port
        attitude_samples.append(AttitudeSample(_dt(m), pitch, roll))

    trips = build_trips(
        fixes, sogs, engine_samples, attitude_samples=attitude_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.roll_variation_deg == pytest.approx(8.0, abs=0.2)
    assert trip.pitch_variation_deg == pytest.approx(3.0, abs=0.2)
    # peak-to-peak (max - min): oscillates between +8/-8 and +3/-3
    assert trip.roll_range_deg == pytest.approx(16.0, abs=0.01)
    assert trip.pitch_range_deg == pytest.approx(6.0, abs=0.01)


def test_motion_variation_range_survives_a_single_rough_patch():
    """A trip that's calm except for one short rough patch should still show the full
    peak-to-peak swing, even though the standard deviation gets diluted by the calm majority."""
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    attitude_samples = []
    for m in range(0, 54):
        if 20 <= m < 22:
            roll = 15.0 if m == 20 else -8.0  # one brief rough patch mid-trip
        else:
            roll = 0.0
        attitude_samples.append(AttitudeSample(_dt(m), None, roll))

    trips = build_trips(
        fixes, sogs, engine_samples, attitude_samples=attitude_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.roll_range_deg == pytest.approx(23.0, abs=0.01)  # 15 - (-8)
    assert trip.roll_variation_deg < 5.0  # diluted by the mostly-calm rest of the trip


def test_motion_variation_absent_without_samples():
    fixes, sogs, engine_samples = _build_scenario()
    geocoder = _StubGeocoder()

    trips = build_trips(
        fixes, sogs, engine_samples,
        geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    assert trips[0].roll_variation_deg is None
    assert trips[0].pitch_variation_deg is None
    assert trips[0].roll_range_deg is None
    assert trips[0].pitch_range_deg is None


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
    assert health.warning_first_seen["Low Oil Pressure"] == _dt(20)


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


def test_gap_within_a_stationary_run_does_not_bridge_to_a_later_unrelated_stay():
    """Regression test for a real bug found in practice: a corrupted SD card produced ~10 hours of
    no/garbled data right after a trip's last good position. The sparse readings on both sides of
    the outage (a last low-speed reading right before it, a low-speed reading once data trickled
    back in) both read as "stationary", so the whole 10-hour span stayed one unbroken run with no
    differently-labelled sample for ``_classify_runs`` to split on -- unlike a "moving" run
    swallowing a gap (already handled), nothing split a *stationary* run's own internal gap before
    this fix. A brief real move shortly after data resumed then still counted as a "negligible"
    move by distance alone, so it (and the unrelated stay after it) got merged into what should
    have stayed a separate, later stay -- the trip's reported arrival ended up at a port the boat's
    logged track never actually reached, with its line drawn straight there across the gap, instead
    of stopping at the boat's real last known position before the failure."""
    fixes, sogs, engine_samples = _build_scenario()

    # last two low-speed readings before the SD card failed
    for m in range(54, 54 + 2):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    # ~10 hour gap: no data at all
    gap_end_minute = 54 + 2 + 600

    # data trickles back in: one low-speed reading far away, then a brief move, then a real stay
    fixes.append(PositionFix(_dt(gap_end_minute), 52.55, 5.20))
    sogs.append(SogSample(_dt(gap_end_minute), 0.0))
    engine_samples.append(EngineSample(_dt(gap_end_minute), 0, 0.5, 3600 * 100 + gap_end_minute * 60))

    nudge_start = gap_end_minute + 1
    for m in range(nudge_start, nudge_start + 2):
        fixes.append(PositionFix(_dt(m), 52.5502, 5.2002))
        sogs.append(SogSample(_dt(m), 2.0))
        engine_samples.append(EngineSample(_dt(m), 0, 3.0, 3600 * 100 + m * 60))

    real_stay_start = nudge_start + 2
    for m in range(real_stay_start, real_stay_start + 24):
        fixes.append(PositionFix(_dt(m), 52.5502, 5.2002))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1
    # arrival stays at the last known position before the gap, not the unrelated later stay
    assert trips[0].arrive_lat == pytest.approx(52.40)
    assert trips[0].arrive_lon == pytest.approx(4.95)
    # the drawn track doesn't reach across the gap either
    assert trips[0].track[-1].lat == pytest.approx(52.40)
    assert trips[0].track[-1].lon == pytest.approx(4.95)


def test_negligible_trip_between_two_stays_is_folded_into_one_combined_stay():
    """Regression test for a real bug found in practice: after mooring, the boat briefly used the
    engine to nudge a few meters further along the quay, then stayed put again. Before the fix,
    that few-meter move was built as its own too-short "trip" and correctly filtered out of the
    logbook -- but the two stays on either side of it were never reconnected, so the *first* stay
    (the boat's position right after arriving, before the nudge) was reported as the trip's
    arrival, silently dropping its actual final position. The fix folds the too-short move back
    into a single combined stay, so the reported arrival reflects the boat's real final spot --
    and (see _track_reaching_markers) splices a synthetic point at the marker's own position onto
    the trip's track, so the line drawn on the map always reaches the arrival marker exactly."""
    fixes, sogs, engine_samples = _build_scenario()

    # first stay: 24 minutes at the original mooring spot (well above min_stop_minutes)
    for m in range(54, 54 + 24):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    # brief engine-on nudge (~15 m, well under min_trip_distance_nm) to the final spot alongside the quay
    nudge_start = 54 + 24
    for m in range(nudge_start, nudge_start + 2):
        fixes.append(PositionFix(_dt(m), 52.4002, 4.9502))
        sogs.append(SogSample(_dt(m), 2.0))  # above speed_threshold_kn -- registers as "moving"
        engine_samples.append(EngineSample(_dt(m), 0, 3.0, 3600 * 100 + m * 60))

    # second stay: 24 more minutes at the final spot
    second_stay_start = nudge_start + 2
    for m in range(second_stay_start, second_stay_start + 24):
        fixes.append(PositionFix(_dt(m), 52.4002, 4.9502))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    # no separate mini-trip for the nudge -- just the original trip
    assert len(trips) == 1
    # the reported arrival is pulled toward the final spot, not stuck at the first stay
    assert trips[0].arrive_lat > 52.40
    assert trips[0].arrive_lon > 4.95
    # the drawn track always ends exactly on the arrival marker, whatever its exact position is
    assert trips[0].track[-1].lat == pytest.approx(trips[0].arrive_lat)
    assert trips[0].track[-1].lon == pytest.approx(trips[0].arrive_lon)


def test_same_stay_looks_right_as_a_departure_but_needs_the_marker_splice_as_an_arrival():
    """Regression test for a real report: the same physical stay (Les Sables-d'Olonne, reached
    after a difficult, wave-tossed approach) looked wrong as an *arrival* but fine as the very
    next trip's *departure* -- confirmed, on the real data, to be the same settled position both
    times (sub-metre difference, pure floating-point rounding), with an 8.4 m gap between the
    arriving trip's own last raw GPS point and that marker (still drifting right up to the last
    sample) versus only 0.6 m for the departing trip's first point (naturally close to where the
    boat just was). This scenario reproduces that shape directly: an arrival whose last sample is
    still meaningfully off from the stay's own averaged centre, immediately followed by a
    departure whose first sample already sits exactly on it."""
    fixes = []
    sogs = []
    engine_samples = []

    def add(m, lat, lon, sog, fuel):
        fixes.append(PositionFix(_dt(m), lat, lon))
        sogs.append(SogSample(_dt(m), sog))
        engine_samples.append(EngineSample(_dt(m), 0, fuel, 3600 * 100 + m * 60))

    # first stay: 12 min at the departure port
    for m in range(0, 12):
        add(m, 52.30, 4.90, 0.0, 0.5)

    # arrival leg: 30 min underway, still drifting/circling right up to its very last sample --
    # well short of the stay it's about to settle into (~90 m away)
    for i, m in enumerate(range(12, 42)):
        frac = i / 29
        add(m, 52.30 + 0.1008 * frac, 4.90 + 0.0508 * frac, 3.0, 8.0)

    # the stay itself (Les Sables-d'Olonne, in the real report): 12 min, settled exactly here
    for m in range(42, 54):
        add(m, 52.4000, 4.9500, 0.0, 0.5)

    # departure leg: first sample already sits exactly on the stay's own position -- naturally
    # close, since the boat was just there
    for i, m in enumerate(range(54, 84)):
        frac = i / 29
        add(m, 52.4000 + 0.1 * frac, 4.9500 + 0.1 * frac, 3.0, 8.0)

    # final stay
    for m in range(84, 96):
        add(m, 52.50, 5.00, 0.0, 0.5)

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 2
    arriving, departing = trips

    # the shared stay's marker is the same real position both times (sub-metre float noise only)
    assert arriving.arrive_lat == pytest.approx(departing.depart_lat, abs=1e-6)
    assert arriving.arrive_lon == pytest.approx(departing.depart_lon, abs=1e-6)
    assert arriving.arrive_lat == pytest.approx(52.4000)
    assert arriving.arrive_lon == pytest.approx(4.9500)

    # the arrival's own last raw sample was genuinely away from the marker -- a real gap existed
    # before the splice
    raw_last = fixes[41]
    assert (raw_last.lat, raw_last.lon) != (arriving.arrive_lat, arriving.arrive_lon)

    # the fix closes it: the drawn arrival track always ends exactly on the marker, with the
    # original raw point preserved right before it
    assert arriving.track[-1].lat == pytest.approx(arriving.arrive_lat)
    assert arriving.track[-1].lon == pytest.approx(arriving.arrive_lon)
    assert arriving.track[-2].lat == pytest.approx(raw_last.lat)
    assert arriving.track[-2].lon == pytest.approx(raw_last.lon)

    # the departure's first sample already matched the marker -- no synthetic point needed, and
    # none was added (track length unchanged at the start)
    assert departing.track[0].lat == pytest.approx(departing.depart_lat)
    assert departing.track[0].lon == pytest.approx(departing.depart_lon)
    assert departing.track[0].lat == pytest.approx(fixes[54].lat)
    assert departing.track[0].lon == pytest.approx(fixes[54].lon)


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


def test_arrival_position_uses_only_the_final_resting_stretch_after_a_folded_intermediate_stop():
    """Regression test for a real bug found on live data (Port du Crouesty, 2026-09-07): the boat
    stopped at an intermediate spot (engine off, ~11 min), then the engine came back on for a
    short transit (~0.14 nm, folded into one continuous trip by --min-leg-distance-nm) to its
    actual final berth, where the engine went off again for good. Folding the short transit leg
    also merges the stationary run on either side of it into one "stay" (see
    _merge_negligible_trips's own stationary-merge branch) -- every sample from the intermediate
    stop was just as much "engine off" as the real final berth, so _settled_position's plain
    engine-on exclusion couldn't tell them apart, and averaged the reported arrival position to
    roughly halfway between two genuinely different places (confirmed on the real data: about
    70 m off from the boat's actual, confirmed berth). Only samples after the *last* engine
    restart -- see _settled_position's own fix -- are the boat's real final resting position."""
    fixes = []
    sogs = []
    engine_samples = []

    def add(m, lat, sog, fuel):
        fixes.append(PositionFix(_dt(m), lat, 4.90))
        sogs.append(SogSample(_dt(m), sog))
        engine_samples.append(EngineSample(_dt(m), 0, fuel, 3600 * 100 + m * 60))

    for m in range(0, 12):
        add(m, 52.30, 0.0, 0.0)  # depart, port A
    for i, m in enumerate(range(12, 42)):
        add(m, 52.30 + 0.10 * (i / 29), 3.0, 8.0)  # underway, engine on
    for m in range(42, 54):
        add(m, 52.40, 0.0, 0.0)  # intermediate stop, engine off, 12 min
    for i, m in enumerate(range(54, 57)):
        # short transit (~0.14 nm, safely under --min-leg-distance-nm's 0.2 nm default, safely
        # over --min-trip-distance-nm's own 0.1 nm), engine back on
        add(m, 52.40 + 0.002246 * (i / 2), 2.0, 3.0)
    for m in range(57, 69):
        add(m, 52.402246, 0.0, 0.0)  # the boat's real final berth, engine off for good, 12 min

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
        min_leg_distance_nm=0.2, lock_radius_m=10.0,
    )

    assert len(trips) == 1  # the intermediate stop no longer shows up as its own separate trip
    # Not the halfway point (52.401123) -- the boat's own actual final berth.
    assert trips[0].arrive_lat == pytest.approx(52.402246, abs=1e-6)


def test_arrival_position_stays_the_full_average_when_an_engine_restart_did_not_move_the_boat():
    """Regression test for a real bug found on live data (La Baule-Escoublac, 2026-09-05), right
    after the fix above: the engine cycled back on for a few minutes while already moored (e.g.
    running a generator/charging batteries), with the boat's SOG never once leaving 0.0 the whole
    stay -- so there's no folded intermediate stop here at all, just one continuous stay whose
    engine happened to restart briefly in the middle. The reported position barely moved (a few
    metres, real GPS noise) across that restart. Narrowing to "samples after the last restart"
    regardless would have traded this stay's full ~1000+ samples for just the handful after the
    restart, for no real gain -- the position genuinely never went anywhere. Only narrows when
    the position before vs. after the restart differs by more than lock_radius_m."""
    fixes = []
    sogs = []
    engine_samples = []

    def add(m, lat, sog, fuel):
        fixes.append(PositionFix(_dt(m), lat, 4.90))
        sogs.append(SogSample(_dt(m), sog))
        engine_samples.append(EngineSample(_dt(m), 0, fuel, 3600 * 100 + m * 60))

    for m in range(0, 12):
        add(m, 52.30, 0.0, 0.0)  # depart, port A
    for i, m in enumerate(range(12, 42)):
        add(m, 52.30 + 0.10 * (i / 29), 3.0, 8.0)  # underway, engine on
    for m in range(42, 54):
        add(m, 52.40, 0.0, 0.0)  # moored, engine off, 12 min -- SOG stays 0.0 throughout
    for m in range(54, 57):
        add(m, 52.40, 0.0, 3.0)  # engine restarts briefly (generator/charging), boat doesn't move
    for m in range(57, 69):
        # engine off again -- position barely different (~8 m, real GPS noise), not a real move
        # -- safely under lock_radius_m=10.0
        add(m, 52.40007, 0.0, 0.0)

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
        lock_radius_m=10.0,
    )

    assert len(trips) == 1
    # The full stay's own average (15 of 27 samples at 52.40, dominant) -- not narrowed down to
    # just the 12 samples after the restart, which alone would average much closer to 52.40007.
    assert trips[0].arrive_lat == pytest.approx(52.40, abs=4e-5)


def test_min_leg_distance_nm_folds_an_in_harbour_reposition_into_one_trip():
    """Regression test for a real bug found with real data: after arriving and mooring briefly,
    the boat repositioned about 0.3 nm within the same harbour (a real, non-negligible move by
    --min-trip-distance-nm's own default of 0.1 nm, so it wasn't filtered as GPS/speed noise) to
    its actual berth -- splitting what was really one continuous arrival into two logbook trips.
    --min-leg-distance-nm (default 0.5 nm, deliberately looser than --min-trip-distance-nm) folds
    an in-transit leg this short back into its surrounding stay."""
    fixes, sogs, engine_samples = _build_scenario()
    for m in range(54, 54 + 12):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))
    fixes.append(PositionFix(_dt(66), 52.40, 4.95))  # still at the old spot, first moving reading
    sogs.append(SogSample(_dt(66), 2.0))
    engine_samples.append(EngineSample(_dt(66), 0, 3.0, 3600 * 100 + 66 * 60))
    fixes.append(PositionFix(_dt(67), 52.405, 4.95))  # ~0.3 nm further into the harbour
    sogs.append(SogSample(_dt(67), 2.0))
    engine_samples.append(EngineSample(_dt(67), 0, 3.0, 3600 * 100 + 67 * 60))
    for m in range(68, 68 + 12):
        fixes.append(PositionFix(_dt(m), 52.405, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))
        engine_samples.append(EngineSample(_dt(m), 0, 0.5, 3600 * 100 + m * 60))

    geocoder = _StubGeocoder()

    # Unset (defaults to --min-trip-distance-nm's own value, same as before this feature existed):
    # the 0.3 nm reposition is a real trip on its own, splitting the visit in two.
    trips_default = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )
    assert len(trips_default) == 2

    # With --min-leg-distance-nm=0.5 (the CLI's own default): folded into one continuous arrival.
    trips_with_leg_distance = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
        min_leg_distance_nm=0.5,
    )
    assert len(trips_with_leg_distance) == 1
    assert trips_with_leg_distance[0].arrive_place.startswith("Port@52.40")


def test_noisy_low_speed_blip_is_folded_by_spatial_spread_even_though_its_path_length_adds_up():
    """Regression test for a real bug found on live data (La Baule-Escoublac, 2026-09-05): a boat
    already back at its berth, engine idling, had SOG noise reading just above
    speed_threshold_kn for several minutes while GPS jitter moved its reported position back and
    forth by only a few meters each step -- individually tiny, but summed over many samples
    (_trip_distance_nm) that add up to more than min_leg_distance_nm, even though the boat was
    never meaningfully away from where it started. Showed up as its own bogus 9-minute "trip"
    with no real destination. lock_radius_m (already used for lock/bridge detection) also folds a
    "moving" run whose own _spatial_spread_m never exceeds it, regardless of summed path length --
    a metric that isn't fooled by many small jittery steps the way summed distance is."""
    fixes = []
    sogs = []

    for m in range(0, 12):  # confirmed stop, port A
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))

    # 60 samples oscillating between two points ~4.5 m apart, SOG just above the default
    # speed_threshold_kn=0.5 -- summed path length ~262 m (0.14 nm, over min_leg_distance_nm's
    # own 0.1 nm default), but never more than ~2.2 m from the midpoint between them.
    for m in range(12, 72):
        lat = 52.30 if (m - 12) % 2 == 0 else 52.30 + 0.00004
        fixes.append(PositionFix(_dt(m), lat, 4.90))
        sogs.append(SogSample(_dt(m), 0.6))

    for m in range(72, 84):  # confirmed stop again, same berth
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, [], geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
        lock_radius_m=10.0,
    )

    assert trips == []  # no bogus trip -- folded entirely into one continuous stay


def test_noisy_low_speed_blip_is_not_folded_when_lock_radius_m_is_none():
    """Same scenario as above, but without lock/bridge detection opted in (lock_radius_m=None,
    build_trips()'s own default): the new spatial-spread check stays off, same as before this fix
    -- only the existing summed-path-length check applies, and that one doesn't catch this blip
    (its summed distance is, deliberately, over min_leg_distance_nm's own default)."""
    fixes = []
    sogs = []

    for m in range(0, 12):
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))

    for m in range(12, 72):
        lat = 52.30 if (m - 12) % 2 == 0 else 52.30 + 0.00004
        fixes.append(PositionFix(_dt(m), lat, 4.90))
        sogs.append(SogSample(_dt(m), 0.6))

    for m in range(72, 84):
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, [], geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 1  # unchanged from before this fix -- the blip still shows as its own trip


def test_real_short_there_and_back_trip_is_not_folded_despite_near_zero_net_displacement():
    """Regression guard for the risk this fix could otherwise introduce: a genuine short trip that
    returns to its own starting berth (net displacement ~0, same as the noise-blip case above) must
    not disappear just because it ends up back where it started -- _spatial_spread_m (unlike a
    naive start-vs-end displacement check) measures how far the boat actually got from its own
    centroid at any point, so a real ~200 m excursion is never confused with a few meters of
    dockside GPS jitter, however the trip's own net displacement compares."""
    fixes = []
    sogs = []

    for m in range(0, 12):  # depart, port A
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))

    # ~200 m out, then back to the exact same spot -- one continuous "moving" run the whole time.
    for i, m in enumerate(range(12, 27)):
        frac = i / 14
        fixes.append(PositionFix(_dt(m), 52.30 + 0.0018 * frac, 4.90))
        sogs.append(SogSample(_dt(m), 3.0))
    for i, m in enumerate(range(27, 42)):
        frac = i / 14
        fixes.append(PositionFix(_dt(m), 52.30 + 0.0018 * (1 - frac), 4.90))
        sogs.append(SogSample(_dt(m), 3.0))

    for m in range(42, 54):  # arrive back, same berth
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, [], geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10,
        lock_radius_m=10.0,
    )

    assert len(trips) == 1  # a real excursion, not folded away
    assert trips[0].depart_place.startswith("Port@52.30")
    assert trips[0].arrive_place.startswith("Port@52.30")  # back at the same berth, still a real trip


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


def _lock_scenario(anchor_drift: bool = False):
    """Port A -> underway -> a stop mid-trip (engine off, minimal drift unless ``anchor_drift``)
    -> underway again -> Port B."""
    fixes = []
    sogs = []
    engine_samples = []

    def add(m, lat, lon, sog, fuel):
        fixes.append(PositionFix(_dt(m), lat, lon))
        sogs.append(SogSample(_dt(m), sog))
        engine_samples.append(EngineSample(_dt(m), 0, fuel, 3600 * 100 + m * 60))

    for m in range(0, 12):
        add(m, 52.30, 4.90, 0.0, 0.0)  # port A, 11 min
    for i, m in enumerate(range(12, 22)):
        add(m, 52.30 + 0.05 * (i / 9), 4.90, 3.0, 8.0)  # underway to the stop, arrives at 52.35
    for i, m in enumerate(range(22, 42)):
        lat = 52.35 + (0.0006 * (i / 19) if anchor_drift else 0.0)
        add(m, lat, 4.90, 0.0, 0.0)  # engine off for 20 min; ~66 m drift if anchor_drift
    end_lat = 52.35 + (0.0006 if anchor_drift else 0.0)
    for i, m in enumerate(range(42, 57)):
        add(m, end_lat + 0.10 * (i / 14), 4.90, 3.0, 8.0)  # underway again, same trip
    for m in range(57, 68):
        add(m, end_lat + 0.10, 4.90, 0.0, 0.0)  # port B, 11 min

    return fixes, sogs, engine_samples


def test_lock_pause_is_folded_into_trip():
    """Engine off, minimal drift (well under --lock-radius-m), and it comes back on later the
    same trip -- treated as a lock/opening bridge, not a port visit."""
    fixes, sogs, engine_samples = _lock_scenario(anchor_drift=False)
    geocoder = _StubGeocoder()

    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder,
        speed_threshold_kn=0.5, min_stop_minutes=10,
        lock_radius_m=10.0, lock_max_duration_minutes=120.0,
    )

    assert len(trips) == 1  # the lock pause doesn't show up as a separate port visit
    assert trips[0].depart_place.startswith("Port@52.30")
    assert trips[0].arrive_place.startswith("Port@52.45")


def test_lock_detection_disabled_by_default():
    """The same scenario as above, but without opting in: build_trips() keeps the old, purely
    speed-based behavior -- a 20-minute stop is a real port visit."""
    fixes, sogs, engine_samples = _lock_scenario(anchor_drift=False)
    geocoder = _StubGeocoder()

    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder,
        speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 2


def test_anchor_stop_with_drift_is_not_folded_as_a_lock():
    """Engine off and on again, same as a lock, but the boat drifted well beyond
    --lock-radius-m (e.g. swinging at anchor) -- still counts as a real stop."""
    fixes, sogs, engine_samples = _lock_scenario(anchor_drift=True)
    geocoder = _StubGeocoder()

    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder,
        speed_threshold_kn=0.5, min_stop_minutes=10,
        lock_radius_m=10.0, lock_max_duration_minutes=120.0,
    )

    assert len(trips) == 2  # the drift disqualifies it as a lock


def test_confined_stop_with_engine_running_is_folded_like_a_lock():
    """Regression test for a real bug found with real data: a boat arriving at a crowded marina
    circled in place for ~10 minutes waiting for a berth to free up, engine kept running the
    whole time (never actually switched off, unlike a lock/bridge pause), then moved a short
    distance to the berth itself -- split what was really one continuous arrival into two
    separate logbook trips. _engine_off_span finds no off-then-on cycle to widen here (the
    engine never goes off), so this only reaches the fallback branch in _reclassify_locks that
    checks the stop's own GPS-measured boundaries directly."""
    fixes = []
    sogs = []
    engine_samples = []

    def add(m, lat, lon, sog, fuel):
        fixes.append(PositionFix(_dt(m), lat, lon))
        sogs.append(SogSample(_dt(m), sog))
        engine_samples.append(EngineSample(_dt(m), 0, fuel, 3600 * 100 + m * 60))

    for m in range(0, 12):
        add(m, 52.30, 4.90, 0.0, 0.0)  # port A, 11 min
    for i, m in enumerate(range(12, 42)):
        add(m, 52.30 + 0.10 * (i / 29), 4.90, 3.0, 8.0)  # underway to the marina
    for m in range(42, 54):
        add(m, 52.40, 4.90, 0.2, 2.0)  # circling for a berth, 11 min (>= min_stop_minutes on its
        # own, so this specifically exercises _reclassify_locks's engine-still-running fallback,
        # not just the plain too-short-to-count check _merge_short_stops already does), engine
        # idling (not off)
    for i, m in enumerate(range(54, 57)):
        add(m, 52.40 + 0.003 * (i / 2), 4.90, 0.6, 3.0)  # short hop to the actual berth (~0.18
        # nm, safely clear of the default min_trip_distance_nm=0.1 filter -- this test's own
        # variable is lock/bridge detection, not the separate negligible-distance filter)
    for m in range(57, 69):
        add(m, 52.403, 4.90, 0.0, 0.0)  # the actual berth, 11 min, engine off

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder,
        speed_threshold_kn=0.5, min_stop_minutes=10,
        lock_radius_m=10.0, lock_max_duration_minutes=120.0,
    )

    assert len(trips) == 1  # the wait-for-a-berth pause doesn't show up as a separate port visit
    assert trips[0].depart_place.startswith("Port@52.30")
    assert trips[0].arrive_place.startswith("Port@52.40")


def test_confined_stop_with_engine_running_stays_a_real_stop_without_lock_detection():
    """Same scenario as above, but without opting in to lock/bridge detection: the old, purely
    speed-based behavior stays unchanged -- a 10-minute stop is a real port visit."""
    fixes = []
    sogs = []
    engine_samples = []

    def add(m, lat, lon, sog, fuel):
        fixes.append(PositionFix(_dt(m), lat, lon))
        sogs.append(SogSample(_dt(m), sog))
        engine_samples.append(EngineSample(_dt(m), 0, fuel, 3600 * 100 + m * 60))

    for m in range(0, 12):
        add(m, 52.30, 4.90, 0.0, 0.0)
    for i, m in enumerate(range(12, 42)):
        add(m, 52.30 + 0.10 * (i / 29), 4.90, 3.0, 8.0)
    for m in range(42, 54):
        add(m, 52.40, 4.90, 0.2, 2.0)
    for i, m in enumerate(range(54, 57)):
        add(m, 52.40 + 0.003 * (i / 2), 4.90, 0.6, 3.0)
    for m in range(57, 69):
        add(m, 52.403, 4.90, 0.0, 0.0)

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder,
        speed_threshold_kn=0.5, min_stop_minutes=10,
    )

    assert len(trips) == 2


def test_confined_stop_with_engine_running_is_not_folded_when_it_is_the_last_run():
    """Regression guard, mirrors test_lock_check_never_applies_to_a_stop_the_engine_never_restarts_from
    for the engine-running case: a confined stop with nothing following it in the data (the log
    simply ends there) must never be folded away, however short and tight it looks -- there's no
    way to tell "still waiting" from "arrived here for good"."""
    fixes = []
    sogs = []
    engine_samples = []

    def add(m, lat, lon, sog, fuel):
        fixes.append(PositionFix(_dt(m), lat, lon))
        sogs.append(SogSample(_dt(m), sog))
        engine_samples.append(EngineSample(_dt(m), 0, fuel, 3600 * 100 + m * 60))

    for m in range(0, 12):
        add(m, 52.30, 4.90, 0.0, 0.0)
    for i, m in enumerate(range(12, 42)):
        add(m, 52.30 + 0.10 * (i / 29), 4.90, 3.0, 8.0)
    for m in range(42, 54):
        add(m, 52.40, 4.90, 0.2, 2.0)  # confined, engine idling, 11 min -- but the log ends right here

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder,
        speed_threshold_kn=0.5, min_stop_minutes=10,
        lock_radius_m=10.0, lock_max_duration_minutes=120.0,
    )

    assert len(trips) == 1
    assert trips[0].arrive_place.startswith("Port@52.40")  # kept as a real arrival, not folded away


def test_lock_check_does_not_apply_to_the_first_stop_in_the_data():
    """A lock only ever happens mid-voyage -- the very first stop in the log (nothing sailed
    before it) must never be folded away, however short and tight it looks."""
    fixes = []
    sogs = []
    engine_samples = []

    def add(m, lat, lon, sog, fuel):
        fixes.append(PositionFix(_dt(m), lat, lon))
        sogs.append(SogSample(_dt(m), sog))
        engine_samples.append(EngineSample(_dt(m), 0, fuel, 3600 * 100 + m * 60))

    for m in range(0, 20):
        add(m, 52.30, 4.90, 0.0, 0.0)  # the log starts already sitting at a berth, engine off
    for i, m in enumerate(range(20, 35)):
        add(m, 52.30 + 0.05 * (i / 14), 4.90, 3.0, 8.0)  # then a real trip departs
    for m in range(35, 46):
        add(m, 52.35, 4.90, 0.0, 0.0)  # port B

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder,
        speed_threshold_kn=0.5, min_stop_minutes=10,
        lock_radius_m=10.0, lock_max_duration_minutes=120.0,
    )

    assert len(trips) == 1
    assert trips[0].depart_place.startswith("Port@52.30")  # not "Unknown (start outside log file)"


def test_lock_check_never_applies_to_a_stop_the_engine_never_restarts_from():
    """Regression guard: a stop with no confirmed engine restart afterwards (at least within the
    available data) must never be folded away, no matter how short and tight it looks -- a lock
    implies the engine comes back on once through it; without that, it's either a genuine
    arrival or simply where the log ends."""
    fixes, sogs, engine_samples = _lock_scenario(anchor_drift=False)
    # truncate right after the lock-like pause ends, before the boat gets underway again --
    # from the engine's perspective this looks identical to a real, final arrival
    cutoff = _dt(42)
    fixes = [f for f in fixes if f.time < cutoff]
    sogs = [s for s in sogs if s.time < cutoff]
    engine_samples = [e for e in engine_samples if e.time < cutoff]

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder,
        speed_threshold_kn=0.5, min_stop_minutes=10,
        lock_radius_m=10.0, lock_max_duration_minutes=120.0,
    )

    assert len(trips) == 1
    assert trips[0].arrive_place.startswith("Port@52.35")  # kept as a real arrival, not folded away


def test_long_stop_fragmented_by_sog_noise_still_recognized():
    """Regression test for a real bug found with real data: a long real stop (engine off the
    whole time) whose *tail end* happens to look short and tight to noisy SOG data -- a single
    spurious "moving" reading (GPS/SOG noise while moored) splits it into fragments -- must still
    be recognized as a real port visit, not folded in as a lock. The lock check has to look at
    the full time the engine was off (_engine_off_span), not just the final SOG-based fragment.
    Found in practice: an overnight stop of 23+ hours whose last 44 minutes, right before the
    engine restarted, moved less than 5 meters and were briefly mistaken for a lock."""
    fixes = []
    sogs = []
    engine_samples = []

    def add(m, lat, lon, sog, fuel):
        fixes.append(PositionFix(_dt(m), lat, lon))
        sogs.append(SogSample(_dt(m), sog))
        engine_samples.append(EngineSample(_dt(m), 0, fuel, 3600 * 100 + m * 60))

    for m in range(0, 12):
        add(m, 52.30, 4.90, 0.0, 0.0)  # port A, 11 min
    for i, m in enumerate(range(12, 22)):
        add(m, 52.30 + 0.05 * (i / 9), 4.90, 3.0, 8.0)  # underway to port X
    for m in range(22, 112):
        add(m, 52.35, 4.90, 0.0, 0.0)  # port X, engine off, part 1 (90 min)
    add(112, 52.35, 4.90, 1.0, 0.0)  # spurious SOG blip; engine still off
    for m in range(113, 172):
        add(m, 52.35, 4.90, 0.0, 0.0)  # port X, engine still off, part 2 (59 min) -- this
        # fragment alone looks lock-like (well under 120 min, no drift)
    for i, m in enumerate(range(172, 187)):
        add(m, 52.35 + 0.10 * (i / 14), 4.90, 3.0, 8.0)  # underway to port Y
    for m in range(187, 198):
        add(m, 52.45, 4.90, 0.0, 0.0)  # port Y

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder,
        speed_threshold_kn=0.5, min_stop_minutes=10,
        lock_radius_m=10.0, lock_max_duration_minutes=120.0,
    )

    assert len(trips) == 2  # port X stays a real port visit, despite the noisy tail fragment
    assert trips[0].arrive_place.startswith("Port@52.35")
    assert trips[1].depart_place.startswith("Port@52.35")


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


def test_engine_hours_extends_into_continuous_running_before_and_after_the_trip():
    """"Gelogde motoruren" is otherwise clipped tightly to the GPS-based trip window -- but on a
    boat where the engine is started at the dock before departure and left running for a bit
    after arrival, that misses real engine time and reads as inconsistent with the engine's own
    hour-meter total (found in practice). It should extend into however long the engine ran
    continuously right on either side of the trip."""
    fixes, sogs, engine_samples = [], [], []

    def add(m, lat, lon, sog, fuel):
        fixes.append(PositionFix(_dt(m), lat, lon))
        sogs.append(SogSample(_dt(m), sog))
        engine_samples.append(EngineSample(_dt(m), 0, fuel, 3600 * 100 + m * 60))

    for m in range(0, 9):  # docked at port A, engine off
        add(m, 52.30, 4.90, 0.0, 0.0)
    for m in range(9, 15):  # still docked, but engine started 5 min before departure (at _dt(14))
        add(m, 52.30, 4.90, 0.0, 8.0)
    for i, m in enumerate(range(15, 25)):  # underway (the trip itself)
        frac = i / 9
        add(m, 52.30 + 0.05 * frac, 4.90 + 0.05 * frac, 3.0, 8.0)
    for m in range(25, 30):  # docked at port B, engine kept running 5 min after arrival
        add(m, 52.35, 4.95, 0.0, 8.0)
    for m in range(30, 40):  # engine stopped, rest of the port B stay
        add(m, 52.35, 4.95, 0.0, 0.0)

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 1
    trip = trips[0]
    # depart_time/arrive_time are each the boundary *stationary* sample, one minute off from the
    # first/last *moving* sample (15/24) -- see build_trips: depart_time=prev_stay.end,
    # arrive_time=next_stay.start.
    assert trip.depart_time == _dt(14)
    assert trip.arrive_time == _dt(25)
    # 20 minutes of continuous engine running (9..29), not just the 11-minute GPS trip window
    # (14..25) -- 5 minutes before departure plus 5 minutes after arrival.
    assert trip.engine_hours[0] == pytest.approx(20 / 60, abs=0.01)


def test_engine_hours_extension_splits_a_shared_stay_instead_of_double_counting():
    """Two trips with only a short stay in between, engine running continuously through the
    whole stay (never switched off between them) -- each trip's own extension must stop at the
    stay's midpoint, so the shared time is split fairly between them instead of both trips
    claiming the same minutes."""
    fixes, sogs, engine_samples = [], [], []

    def add(m, lat, lon, sog, fuel):
        fixes.append(PositionFix(_dt(m), lat, lon))
        sogs.append(SogSample(_dt(m), sog))
        engine_samples.append(EngineSample(_dt(m), 0, fuel, 3600 * 100 + m * 60))

    for m in range(0, 14):  # port A, engine off
        add(m, 52.30, 4.90, 0.0, 0.0)
    for m in range(14, 15):  # engine starts right as trip A departs
        add(m, 52.30, 4.90, 0.0, 8.0)
    for i, m in enumerate(range(15, 25)):  # trip A, underway, engine on
        frac = i / 9
        add(m, 52.30 + 0.05 * frac, 4.90 + 0.05 * frac, 3.0, 8.0)
    for m in range(25, 40):  # port B -- the shared stay, engine kept running the whole time
        add(m, 52.35, 4.95, 0.0, 8.0)
    for i, m in enumerate(range(40, 50)):  # trip B, underway, engine on
        frac = i / 9
        add(m, 52.35 + 0.05 * frac, 4.95 + 0.05 * frac, 3.0, 8.0)
    for m in range(50, 51):  # engine kept running 1 more minute right as trip B arrives
        add(m, 52.40, 5.00, 0.0, 8.0)
    for m in range(51, 65):  # rest of port C, engine off
        add(m, 52.40, 5.00, 0.0, 0.0)

    geocoder = _StubGeocoder()
    trips = build_trips(
        fixes, sogs, engine_samples, geocoder=geocoder, speed_threshold_kn=0.5, min_stop_minutes=10
    )

    assert len(trips) == 2
    trip_a, trip_b = trips
    # The engine ran continuously from _dt(14) to _dt(50) (36 minutes) without ever stopping.
    # Port B (the shared stay) spans _dt(25)..(39), midpoint _dt(32): trip A's extension reaches
    # forward to that midpoint, trip B's extension reaches back to the same midpoint -- together
    # accounting for the full 36-minute on-interval exactly once, not twice (18 + 18 = 36).
    assert trip_a.engine_hours[0] == pytest.approx(18 / 60, abs=0.01)
    assert trip_b.engine_hours[0] == pytest.approx(18 / 60, abs=0.01)
