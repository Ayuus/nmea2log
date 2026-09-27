"""Groups the timestamps of dropped/ignored samples into *stretches*, so a log line can say when it
happened: a shutdown and the start-up hours later are two events, not one total. Shared by the GNSS
gate (gnss_gate.py) and the position outlier checks (tripbuilder.py), which each log one short line
per stretch: ``2026-07-30 12:39:12/2026-07-30 12:39:41 UTC``.

Times are the naive UTC datetimes the whole decode pipeline uses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List

# Samples at most this far apart belong to the same stretch (DOP messages come every ~2 s, so one
# no-fix period is settled in many small pieces that must still read as one event).
STRETCH_GAP = timedelta(seconds=30)


@dataclass
class Stretch:
    first: datetime
    last: datetime
    count: int


def format_stretch_times(stretch: Stretch) -> str:
    """Just the times, slash-separated between a first/last pair (a single sample has no second
    time) -- shared by gnss_gate.py's and tripbuilder.py's own short, one-line-per-stretch log
    format. The count is stated by the caller itself, not included here."""
    if stretch.last != stretch.first:
        return f"{stretch.first:%Y-%m-%d %H:%M:%S}/{stretch.last:%Y-%m-%d %H:%M:%S} UTC"
    return f"{stretch.first:%Y-%m-%d %H:%M:%S} UTC"


class StretchLog:
    """The times of the samples one check dropped, kept as stretches. Times must be added in
    non-decreasing order (a check walks the data in time order)."""

    def __init__(self) -> None:
        self.stretches: List[Stretch] = []
        self.total = 0

    def add(self, when: datetime) -> None:
        self.total += 1
        if self.stretches and when - self.stretches[-1].last <= STRETCH_GAP:
            self.stretches[-1].last = when
            self.stretches[-1].count += 1
        else:
            self.stretches.append(Stretch(when, when, 1))
