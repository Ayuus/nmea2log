"""Tiny stdlib logging helper: prefixes every progress line with the current date/time, so a run
against a slow network (downloading from the W2K-2) or a large dataset (parsing hundreds of .ebl
files) makes it visible how much time each step actually costs.

Every message optionally also goes to a persistent log file (see ``set_log_file``) -- console
output alone disappears the moment the terminal window closes, which for a run launched by
double-clicking a .bat file (no separate "did it finish?" step) leaves nothing to check
afterwards, particularly for something like --backup-ebl that can take a while and is easy to
interrupt by closing the window too early (found in practice).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, TextIO

_log_file: Optional[TextIO] = None
_log_sink: Optional[Callable[[str], None]] = None

DEFAULT_LOG_RETENTION_DAYS = 90.0

# "info" (the default) and "debug", in ascending verbosity -- deliberately just two, not a full
# logging-module-style hierarchy: the only distinction this app actually needs is "worth showing
# on a normal run" vs. "routine per-item chatter that's only useful when actively troubleshooting"
# (e.g. one line per already-downloaded .ebl file -- found in practice: a normal day-to-day sync
# with a couple thousand files already local produced that many lines on screen/in the Android
# notification for zero new information, drowning out the handful of lines that actually mattered).
_LEVELS = {"debug": 0, "info": 1}
_console_level = "info"


def set_log_level(level: str) -> None:
    """Changes the minimum level printed to the console/forwarded to the log sink (see
    set_log_sink) -- the log file itself (see set_log_file) always receives every level
    regardless, so lowering this never loses anything from the on-disk troubleshooting history,
    it only trims what's shown live."""
    if level not in _LEVELS:
        raise ValueError(f"unknown log level {level!r} (expected one of {sorted(_LEVELS)})")
    global _console_level
    _console_level = level


def set_log_sink(sink: Optional[Callable[[str], None]]) -> None:
    """Lets a caller receive every log() line as it's produced, on top of the normal print (and
    the log file, if set) -- used by android_entry.py so the Android app can show the exact same
    messages the desktop CLI shows, instead of maintaining a separate set of Android-specific UI
    text. Pass None to clear (android_entry.py does this once a sync finishes, so a later desktop
    test run in the same process -- e.g. under pytest -- doesn't keep forwarding to a stale
    sink)."""
    global _log_sink
    _log_sink = sink


def set_log_file(path: Path, retention_days: float = DEFAULT_LOG_RETENTION_DAYS) -> None:
    """Every subsequent ``log()`` call also gets appended to ``path`` (created if it doesn't
    exist yet), on top of whatever stream it already prints to -- call once, near the start of a
    run. Appends rather than overwriting, so a history of runs accumulates in one place instead
    of only ever showing the most recent one -- but without any cleanup at all that history would
    grow forever, so any existing line older than ``retention_days`` gets dropped first (found in
    practice: nothing was ever trimming this file)."""
    global _log_file
    if path.exists():
        _prune_old_lines(path, retention_days)
    _log_file = path.open("a", encoding="utf-8")


def _prune_old_lines(path: Path, retention_days: float) -> None:
    cutoff = datetime.now() - timedelta(days=retention_days)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = []
    for line in lines:
        # Every line starts with "YYYY-MM-DD HH:MM:SS " (see log() below) -- a line that doesn't
        # parse that way is kept rather than risk silently dropping something unexpected.
        try:
            timestamp = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            kept.append(line)
            continue
        if timestamp >= cutoff:
            kept.append(line)
    if len(kept) != len(lines):
        path.write_text("".join(kept), encoding="utf-8")


def log(message: str, *, file: TextIO = sys.stdout, level: str = "info") -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Every line, not just the first -- a lot of what gets logged here is a multi-line dump of
    # someone else's output (e.g. the sftp client's own error text, banner included), and with
    # only the first line stamped, skimming the log to see when a several-line block actually
    # happened meant hunting upward for the nearest timestamp above it (found in practice, asked
    # for explicitly).
    line = "\n".join(f"{timestamp} {part}" for part in message.split("\n"))
    if _log_file is not None:
        # Flushed immediately, not just on process exit -- if the process gets killed abruptly
        # (e.g. the terminal window closed mid-run, the exact scenario this file exists to help
        # diagnose), whatever was logged right up to that point should still be on disk. Written
        # regardless of _console_level -- this file is the one place the full detail always
        # survives, console/sink filtering is purely about what's shown *live*.
        print(line, file=_log_file, flush=True)
    if _LEVELS[level] < _LEVELS[_console_level]:
        return
    print(line, file=file)
    if _log_sink is not None:
        _log_sink(line)
