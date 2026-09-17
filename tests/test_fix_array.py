from datetime import datetime

from nmea2log.fix_array import FixArray, SogArray
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
