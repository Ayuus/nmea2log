"""Values both apps must keep equal: the log's limits and tags, and the names of the files in the app's
folder. The Android app gets them as a generated Kotlin file (export_android_constants.py), the iOS app imports
them; before this each app had its own copy of every number ("kept in sync by hand")."""

from __future__ import annotations

# Lines of the log, in the order they are tested: a line holding any of the first tags is an error (shown
# red), then any of the second a warning (amber). "[geocode]" lines are all failed lookups.
LOG_ERROR_TAGS = ("[error]",)
LOG_WARNING_TAGS = ("[warning]", "[anomaly]", "[geocode]")

# What log.py puts in front of every line.
LOG_TIMESTAMP_REGEX = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "

# The log view keeps this many lines; past MAX it drops the oldest down to TRIM_TO (nmea2log.log has all).
LOG_MAX_LINES = 200_000
LOG_TRIM_TO = 150_000
# New lines are shown at most this often, however many arrive.
LOG_REFRESH_INTERVAL_MS = 250
# Height of the log strip next to the logbook after a run, in dp / pt.
LOG_STRIP_HEIGHT = 150

# Files in the app's folder.
LOGBOOK_FILE_NAME = "logbook.html"
SAMPLE_CACHE_FILE_NAME = "sample_cache.pkl"
LOG_FILE_NAME = "nmea2log.log"
EBL_DIR_NAME = "Actisense"


def classify_line(line: str) -> str:
    """"error", "warning" or "info" for a log line, as the log view colours it."""
    if any(tag in line for tag in LOG_ERROR_TAGS):
        return "error"
    if any(tag in line for tag in LOG_WARNING_TAGS):
        return "warning"
    return "info"
