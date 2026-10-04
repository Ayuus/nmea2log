import dataclasses
import os
import re
from pathlib import Path

import pytest

from nmea2log import app_constants, app_settings, export_android_constants
from nmea2log.bootmode import BootModeConfig


def test_parse_int_uses_the_default_for_text_that_is_no_number():
    assert app_settings.parse_int("abc", 30) == 30
    assert app_settings.parse_int("", 30) == 30
    assert app_settings.parse_int(None, 30) == 30


def test_parse_int_enforces_the_minimum():
    assert app_settings.parse_int("0", 30, minimum=1) == 1
    assert app_settings.parse_int("-5", 30, minimum=1) == 1
    assert app_settings.parse_int("45", 30, minimum=1) == 45
    assert app_settings.parse_int("0", 30) == 0


def test_parse_float():
    assert app_settings.parse_float("12.5", 10.0) == 12.5
    assert app_settings.parse_float("x", 10.0) == 10.0


def test_completeness_needs_every_field_not_blank():
    assert app_settings.is_w2k2_complete("admin", "pw")
    assert not app_settings.is_w2k2_complete("admin", "  ")
    assert app_settings.is_rest_complete("https://x", "u", "p")
    assert not app_settings.is_rest_complete("https://x", "", "p")
    assert app_settings.is_sftp_complete("h", "u", "p", "/path")
    assert not app_settings.is_sftp_complete("h", "u", "p", "")


def test_rest_is_preferred_over_sftp():
    assert app_settings.publish_method(True, True) == "rest"
    assert app_settings.publish_method(False, True) == "sftp"
    assert app_settings.publish_method(False, False) is None


def test_the_boat_mode_defaults_are_the_ones_of_bootmode_config():
    config = BootModeConfig()
    names = {f.name for f in dataclasses.fields(BootModeConfig)}
    checked = 0
    for key, value in app_settings.DEFAULTS.items():
        if key.startswith("boot_") and key != "boot_auto_start":
            name = key[len("boot_"):]  # boot_round_interval_minutes is bootmode's round_interval_minutes
            assert name in names, name
            assert getattr(config, name) == value, name
            checked += 1
    assert checked == 8


def test_the_default_boat_interval_is_one_of_the_choices():
    assert app_settings.DEFAULTS["boot_round_interval_minutes"] in app_settings.BOOT_INTERVAL_CHOICES


def test_classify_line():
    assert app_constants.classify_line("2026-10-04 15:00:00 [error] boom") == "error"
    assert app_constants.classify_line("2026-10-04 15:00:00 [warning] x") == "warning"
    assert app_constants.classify_line("2026-10-04 15:00:00 [anomaly] x") == "warning"
    assert app_constants.classify_line("2026-10-04 15:00:00 [geocode] x") == "warning"
    assert app_constants.classify_line("2026-10-04 15:00:00 [info] x") == "info"


def test_the_log_timestamp_regex_matches_what_log_puts_in_front():
    assert re.match(app_constants.LOG_TIMESTAMP_REGEX, "2026-10-04 15:00:00 [info] x")
    assert not re.match(app_constants.LOG_TIMESTAMP_REGEX, "[info] x")


def test_the_log_limits_leave_room_between_trim_and_max():
    assert 0 < app_constants.LOG_TRIM_TO < app_constants.LOG_MAX_LINES


def test_the_generated_kotlin_has_the_shared_values():
    text = export_android_constants.render()

    assert "const val LOG_MAX_LINES = 200000" in text
    assert 'val LOG_WARNING_TAGS = listOf("[warning]", "[anomaly]", "[geocode]")' in text
    assert "const val BOOT_ROUND_INTERVAL_MINUTES = 60" in text
    assert "const val DEFAULT_MIN_STOP_MINUTES = 10.0" in text
    assert "const val AUTO_SYNC_ON_LAUNCH = false" in text
    assert "val BOOT_INTERVAL_CHOICES = listOf(30, 60, 120, 180)" in text


@pytest.mark.skipif(
    not os.environ.get("MYSAILINGLOGBOOK_ANDROID_RES"),
    reason="set MYSAILINGLOGBOOK_ANDROID_RES to the Android app's res directory to check its generated file",
)
def test_the_android_shared_constants_file_is_up_to_date():
    res = Path(os.environ["MYSAILINGLOGBOOK_ANDROID_RES"])
    kotlin = res.parent / "java" / "com" / "ayuus" / "mysailinglogbook" / "SharedConstants.kt"

    assert export_android_constants.main([str(kotlin), "--check"]) == 0


def test_stamp_line_stamps_every_line_that_has_no_timestamp():
    from datetime import datetime

    from nmea2log.log import stamp_line

    now = datetime(2026, 10, 4, 15, 30, 5)

    assert stamp_line("[info] hello", now) == "2026-10-04 15:30:05 [info] hello"
    assert stamp_line("a\nb", now) == "2026-10-04 15:30:05 a\n2026-10-04 15:30:05 b"
    already = "2026-10-04 10:00:00 [info] from python"
    assert stamp_line(already, now) == already
    assert stamp_line(already + "\ncontinuation", now) == already + "\n2026-10-04 15:30:05 continuation"
