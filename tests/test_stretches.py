from datetime import datetime, timedelta

from nmea2log.stretches import Stretch, StretchLog, format_stretch_times

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


def test_format_stretch_times_gives_a_slash_separated_date_time_range_utc():
    text = format_stretch_times(Stretch(_T0, _T0 + timedelta(seconds=29), 258))

    assert text == "2026-07-30 12:39:12/2026-07-30 12:39:41 UTC"


def test_format_stretch_times_of_a_single_sample_has_no_slash():
    assert format_stretch_times(Stretch(_T0, _T0, 1)) == "2026-07-30 12:39:12 UTC"
