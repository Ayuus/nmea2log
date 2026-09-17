from datetime import datetime, timedelta

from nmea2log.fix_array import AttitudeArray, FixArray, NavSampleArray, SogArray, _to_epoch
from nmea2log.model import AttitudeSample, PositionFix, SogSample


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


def test_sog_array_sorted_by_time_drops_a_backward_jumping_row():
    """A row whose time is earlier than the row before it is dropped, not reordered into place --
    see sorted_by_time's own docstring: on real data this is a device logging with a stale clock
    before its first real time sync, not legitimate arrival-order jitter, so the wrong reading
    should disappear rather than end up at a different, equally misleading position."""
    base = datetime(2026, 7, 15, 9, 0, 0)
    array = SogArray(
        [
            SogSample(base + timedelta(seconds=1), 1.0, None),
            SogSample(base, 2.0, None),  # earlier than the row before it -- dropped
        ]
    )

    result = array.sorted_by_time()

    assert result is array  # mutated and returned in place, not a new object
    assert [s.time for s in result] == [base + timedelta(seconds=1)]
    assert [s.sog_ms for s in result] == [1.0]


def test_sog_array_sorted_by_time_keeps_rows_with_equal_timestamps():
    """Ties aren't a backward jump -- common in practice, since every frame between two PGN 126992
    updates shares the exact same time (see ebl_reader.py's own docstring); dropping them would
    lose almost all the data instead of just the genuinely corrupted readings."""
    base = datetime(2026, 7, 15, 9, 0, 0)
    array = SogArray([SogSample(base, 1.0, None), SogSample(base, 2.0, None)])

    result = array.sorted_by_time()

    assert [s.sog_ms for s in result] == [1.0, 2.0]


def test_sog_array_sorted_by_time_trusts_a_large_backward_jump_as_a_clock_sync():
    """A backward jump this large (see _CLOCK_JUMP_THRESHOLD_S) is trusted, not dropped like a
    small one -- otherwise every genuinely good row that follows a device's pre-sync clock error
    would also get dropped for "coming before" that wrong, far-future anchor, all the way until
    real time caught back up to it (found in practice, on a real archive: this silently
    discarded nearly a whole season -- 2.7 million fixes in, 27 left)."""
    bad_clock_start = datetime(2026, 7, 15, 9, 0, 0)  # device's own uncorrected clock
    real_time = datetime(2026, 6, 1, 8, 0, 0)  # the real time, weeks earlier
    array = SogArray(
        [
            SogSample(bad_clock_start, 9.0, None),
            SogSample(bad_clock_start + timedelta(seconds=1), 9.5, None),  # still on the bad clock
            SogSample(real_time, 1.0, None),  # the clock-sync correction -- weeks backward
            SogSample(real_time + timedelta(seconds=1), 1.5, None),  # genuinely good data after it
        ]
    )

    result = array.sorted_by_time()

    # The two bad-clock readings stay (a known, accepted limitation -- see the function's own
    # docstring), but critically, both readings *after* the correction are kept too, not dropped
    # for "coming before" the wrong anchor.
    assert [s.sog_ms for s in result] == [9.0, 9.5, 1.0, 1.5]


def test_sog_array_sorted_by_time_is_a_true_noop_when_already_sorted():
    base = datetime(2026, 7, 15, 9, 0, 0)
    original = [SogSample(base, 2.0, None), SogSample(base + timedelta(seconds=1), 1.0, None)]
    array = SogArray(original)

    result = array.sorted_by_time()

    assert result is array
    assert list(result) == original


def test_attitude_array_sorted_by_time_drops_a_backward_jumping_row():
    base = datetime(2026, 7, 15, 9, 0, 0)
    array = AttitudeArray(
        [
            AttitudeSample(base + timedelta(seconds=1), 1.0, 2.0),
            AttitudeSample(base, 3.0, 4.0),  # earlier than the row before it -- dropped
        ]
    )

    result = array.sorted_by_time()

    assert result is array
    assert [s.time for s in result] == [base + timedelta(seconds=1)]
    assert [s.pitch_deg for s in result] == [1.0]


def test_attitude_array_sorted_by_time_trusts_a_large_backward_jump_as_a_clock_sync():
    """See SogArray's own equivalent test for the full reasoning."""
    bad_clock_start = datetime(2026, 7, 15, 9, 0, 0)
    real_time = datetime(2026, 6, 1, 8, 0, 0)
    array = AttitudeArray(
        [
            AttitudeSample(bad_clock_start, 9.0, 1.0),
            AttitudeSample(bad_clock_start + timedelta(seconds=1), 9.5, 1.5),
            AttitudeSample(real_time, 2.0, 3.0),
            AttitudeSample(real_time + timedelta(seconds=1), 2.5, 3.5),
        ]
    )

    result = array.sorted_by_time()

    assert [s.pitch_deg for s in result] == [9.0, 9.5, 2.0, 2.5]
