from datetime import datetime, timedelta

from nmea2log.stretches import MAX_STRETCHES_LOGGED, Stretch, StretchLog, format_stretch

_T0 = datetime(2026, 7, 30, 12, 39, 12)


def test_samples_close_together_form_one_stretch():
    log = StretchLog()
    for seconds in (0, 1, 2, 30):  # each within 30 s of the previous one
        log.add(_T0 + timedelta(seconds=seconds))

    assert log.total == 4
    assert log.stretches == [Stretch(_T0, _T0 + timedelta(seconds=30), 4)]


def test_a_longer_gap_starts_a_new_stretch():
    log = StretchLog()
    log.add(_T0)
    log.add(_T0 + timedelta(hours=2))

    assert [s.count for s in log.stretches] == [1, 1]


def test_format_stretch_gives_date_time_utc_and_count():
    text = format_stretch(Stretch(_T0, _T0 + timedelta(seconds=29), 258))

    assert text == "2026-07-30 12:39:12 until 2026-07-30 12:39:41 UTC (258 fixes)"


def test_format_stretch_of_a_single_sample_has_no_until_and_a_singular_count():
    assert format_stretch(Stretch(_T0, _T0, 1)) == "2026-07-30 12:39:12 UTC (1 fix)"


def test_format_stretch_can_leave_the_count_to_the_caller():
    assert format_stretch(Stretch(_T0, _T0, 5), with_count=False) == "2026-07-30 12:39:12 UTC"


def test_describe_caps_the_number_of_stretches_spelled_out():
    log = StretchLog()
    for hour in range(MAX_STRETCHES_LOGGED + 3):
        log.add(_T0 + timedelta(hours=hour))

    text = log.describe()

    assert text.count(" UTC (1 fix)") == MAX_STRETCHES_LOGGED
    assert text.endswith("and 3 more")
