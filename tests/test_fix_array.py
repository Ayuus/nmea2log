from datetime import datetime

from nmea2log.fix_array import FixArray, SogArray, _to_epoch
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


def test_fix_array_slice_by_time_keeps_only_the_requested_half_open_window():
    fixes = [
        PositionFix(datetime(2026, 7, 15, 8, 0, 0), 52.30, 4.90),
        PositionFix(datetime(2026, 7, 15, 9, 0, 0), 52.31, 4.91),
        PositionFix(datetime(2026, 7, 15, 10, 0, 0), 52.32, 4.92),
        PositionFix(datetime(2026, 7, 15, 11, 0, 0), 52.33, 4.93),
    ]
    array = FixArray(fixes)

    sliced = array.slice_by_time(_to_epoch(fixes[1].time), _to_epoch(fixes[3].time))

    # Half-open: the start boundary is included, the end boundary is not.
    assert list(sliced) == fixes[1:3]


def test_fix_array_slice_by_time_returns_empty_for_a_window_outside_the_data():
    fixes = [PositionFix(datetime(2026, 7, 15, 9, 0, 0), 52.30, 4.90)]
    array = FixArray(fixes)

    sliced = array.slice_by_time(_to_epoch(datetime(2026, 1, 1)), _to_epoch(datetime(2026, 1, 2)))

    assert len(sliced) == 0
    assert list(sliced) == []


def test_sog_array_slice_by_time_keeps_only_the_requested_half_open_window():
    sogs = [
        SogSample(datetime(2026, 7, 15, 8, 0, 0), 1.0, 90.0),
        SogSample(datetime(2026, 7, 15, 9, 0, 0), 2.0, 91.0),
        SogSample(datetime(2026, 7, 15, 10, 0, 0), 3.0, 92.0),
        SogSample(datetime(2026, 7, 15, 11, 0, 0), 4.0, 93.0),
    ]
    array = SogArray(sogs)

    sliced = array.slice_by_time(_to_epoch(sogs[1].time), _to_epoch(sogs[3].time))

    assert list(sliced) == sogs[1:3]
