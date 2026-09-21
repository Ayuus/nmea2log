"""Groups the timestamps of dropped/ignored samples into *stretches*, so a log line can say when it
happened: a shutdown and the start-up hours later are two events, not one total. Shared by the GNSS
gate (gnss_gate.py) and the position outlier checks (tripbuilder.py) so both describe them the same
way: ``2026-07-30 12:39:12 until 2026-07-30 12:39:41 UTC (258 fixes)``.

Times are the naive UTC datetimes the whole decode pipeline uses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List

# Samples at most this far apart belong to the same stretch (DOP messages come every ~2 s, so one
# no-fix period is settled in many small pieces that must still read as one event).
STRETCH_GAP = timedelta(seconds=30)

# Only this many stretches are spelled out in one line (a source that keeps glitching must not
# produce an unbounded line); the rest is summarised as "and N more".
MAX_STRETCHES_LOGGED = 8


@dataclass
class Stretch:
    first: datetime
    last: datetime
    count: int


def format_stretch(stretch: Stretch, with_count: bool = True) -> str:
    """``2026-07-30 12:39:12 until 2026-07-30 12:39:41 UTC (258 fixes)`` (a single sample has no
    "until"; ``with_count=False`` leaves the count off for a caller that states it itself)."""
    text = f"{stretch.first:%Y-%m-%d %H:%M:%S}"
    if stretch.last != stretch.first:
        text += f" until {stretch.last:%Y-%m-%d %H:%M:%S}"
    text += " UTC"
    if with_count:
        text += f" ({stretch.count} {'fix' if stretch.count == 1 else 'fixes'})"
    return text


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

    def describe(self) -> str:
        """All stretches in one string, capped at MAX_STRETCHES_LOGGED."""
        parts = [format_stretch(s) for s in self.stretches[:MAX_STRETCHES_LOGGED]]
        more = len(self.stretches) - MAX_STRETCHES_LOGGED
        if more > 0:
            parts.append(f"and {more} more")
        return "; ".join(parts)
