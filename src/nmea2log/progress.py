"""Recognises the progress lines the pipeline logs, so the apps get (phase, current, total) instead of each
parsing the log text with a copy of the same regexes.

Decoding logs "...decoded 42/1940 logfile(s) so far"; building the trips afterwards logs four checkpoints
that have no numbers of their own, always in the same order, so the one that matched counts as "step X of 4".
The regexes live here, next to the log() calls they match (see the tests, which feed the real lines in):
a changed log text can no longer silently break one app's progress bar.
"""

from __future__ import annotations

import re
from typing import Callable, Optional, Tuple

DECODING = "decoding"
BUILDING_TRIPS = "building_trips"

_DECODE = re.compile(r"decoded (\d+)/(\d+) logfile\(s\) so far")
_BUILD_CHECKPOINTS = (
    re.compile(r"Building trips from \d+ GPS position\(s\)"),
    re.compile(r"\d+ navigation samples merged, classifying trips"),
    re.compile(r"\d+ run\(s\) classified, computing per-trip statistics"),
    re.compile(r"\d+ trip\(s\) found, writing logbook"),
)


def parse_progress_line(line: str) -> Optional[Tuple[str, int, int]]:
    """(phase, current, total) a log line stands for, or None when it is not a progress line."""
    decode = _DECODE.search(line)
    if decode:
        return DECODING, int(decode.group(1)), int(decode.group(2))
    for step, checkpoint in enumerate(_BUILD_CHECKPOINTS):
        if checkpoint.search(line):
            return BUILDING_TRIPS, step + 1, len(_BUILD_CHECKPOINTS)
    return None


def sink_with_progress(progress_callback) -> Callable[[str], None]:
    """The log sink for ``progress_callback``: every line goes to its onLogLine(), and the lines that stand
    for progress also to its onProgress(phase, current, total) when it has one."""
    on_progress = getattr(progress_callback, "onProgress", None)

    def sink(line: str) -> None:
        progress_callback.onLogLine(line)
        if on_progress is not None:
            progress = parse_progress_line(line)
            if progress is not None:
                on_progress(*progress)

    return sink
