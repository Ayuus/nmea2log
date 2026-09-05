from datetime import datetime, timedelta

import pytest

from nmea2000processor.model import (
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
from nmea2000processor.tripbuilder import _reject_gps_outliers, build_trips


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


def test_negligible_trip_between_two_stays_is_folded_into_one_combined_stay():
    """Regression test for a real bug found in practice: after mooring, the boat briefly used the
    engine to nudge a few meters further along the quay, then stayed put again. Before the fix,
    that few-meter move was built as its own too-short "trip" and correctly filtered out of the
    logbook -- but the two stays on either side of it were never reconnected, so the *first* stay
    (the boat's position right after arriving, before the nudge) was reported as the trip's
    arrival, silently dropping its actual final position. The fix folds the too-short move back
    into a single combined stay, so the reported arrival reflects the boat's real final spot."""
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
