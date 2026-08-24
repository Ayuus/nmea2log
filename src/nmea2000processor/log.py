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
from typing import Optional, TextIO

_log_file: Optional[TextIO] = None

DEFAULT_LOG_RETENTION_DAYS = 90.0


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


def log(message: str, *, file: TextIO = sys.stdout) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Every line, not just the first -- a lot of what gets logged here is a multi-line dump of
    # someone else's output (e.g. the sftp client's own error text, banner included), and with
    # only the first line stamped, skimming the log to see when a several-line block actually
    # happened meant hunting upward for the nearest timestamp above it (found in practice, asked
    # for explicitly).
    line = "\n".join(f"{timestamp} {part}" for part in message.split("\n"))
    print(line, file=file)
    if _log_file is not None:
        # Flushed immediately, not just on process exit -- if the process gets killed abruptly
        # (e.g. the terminal window closed mid-run, the exact scenario this file exists to help
        # diagnose), whatever was logged right up to that point should still be on disk.
        print(line, file=_log_file, flush=True)
