from datetime import datetime, timedelta

from nmea2log.fix_array import FixArray, NavSampleArray, SogArray, _to_epoch
from nmea2log.model import PositionFix, SogSample


def test_fix_array_round_trips_fixes_preserving_order():
    fixes = [
        PositionFix(datetime(2026, 7, 15, 9, 0, 0), 52.30, 4.90),
        PositionFix(datetime(2026, 7, 15, 9, 0, 1), 52.31, 4.91),
        PositionFix(datetime(2026, 7, 15, 9, 0, 2, 500000), 52.32, 4.92),  # sub-second precision
    ]

    array = FixArray(fixes)

    assert len(array) == 3
    assert list(array) == fixes
    assert array == fixes


def test_fix_array_is_empty_by_default():
    array = FixArray()

    assert len(array) == 0
    assert list(array) == []


def test_fix_array_append_and_indexed_accessors():
    array = FixArray()
    fix = PositionFix(datetime(2026, 7, 15, 9, 0, 0), 52.30, 4.90)

    array.append(fix)

    assert array.lat_at(0) == 52.30
    assert array.lon_at(0) == 4.90
    assert array.datetime_at(0) == fix.time


def test_sog_array_round_trips_including_none_cog():
    sogs = [
        SogSample(datetime(2026, 7, 15, 9, 0, 0), 3.5, 180.0),
        SogSample(datetime(2026, 7, 15, 9, 0, 1), 0.0, None),  # no course over ground
    ]

    array = SogArray(sogs)

    assert len(array) == 2
    assert list(array) == sogs
    assert array == sogs


def test_nav_sample_array_round_trips_raw_columns_including_none_fields():
    array = NavSampleArray()

    array.append_raw(0.0, 52.30, 4.90, 1.5, 12.3, 18.5, 90.0)
    array.append_raw(1.0, 52.31, 4.91, 0.0, None, None, None)  # no depth/water-temp/cog reading

    assert len(array) == 2
    assert array.lat_at(0) == 52.30
    assert array.lon_at(0) == 4.90
    assert array.sog_at(0) == 1.5
    assert array.depth_at(0) == 12.3
    assert array.water_temp_at(0) == 18.5
    assert array.cog_at(0) == 90.0
    assert array.depth_at(1) is None
    assert array.water_temp_at(1) is None
    assert array.cog_at(1) is None


def test_nav_sample_array_is_empty_by_default():
    array = NavSampleArray()

    assert len(array) == 0


def test_nav_sample_array_index_range_for_time_is_inclusive_both_ends():
    array = NavSampleArray()
    base = datetime(2026, 7, 15, 9, 0, 0)
    for i in range(5):
        array.append_raw(_to_epoch(base) + i, 0.0, 0.0, 0.0, None, None, None)

    assert array.index_range_for_time(base, base) == (0, 1)
    assert array.index_range_for_time(base + timedelta(seconds=1), base + timedelta(seconds=3)) == (1, 4)


def test_nav_sample_array_index_range_for_time_returns_none_when_nothing_matches():
    array = NavSampleArray()
    base = datetime(2026, 7, 15, 9, 0, 0)
    array.append_raw(_to_epoch(base), 0.0, 0.0, 0.0, None, None, None)

    assert array.index_range_for_time(base + timedelta(hours=1), base + timedelta(hours=2)) is None
