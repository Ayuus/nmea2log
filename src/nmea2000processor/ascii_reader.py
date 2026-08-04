"""Reads Actisense 'N2K ASCII' log files (the format the W2K-2 writes to disk).

Line format (see Actisense's 'NMEA 2000 ASCII Output format' documentation):

    Ahhmmss.ddd SSDDP PPPPP b0b1b2...bn

    A            fixed letter, "message received"
    hhmmss.ddd   time of day (no date!)
    SS           source address, 2 hex digits
    DD           destination address, 2 hex digits
    P            priority 0-7, 1 hex digit
    PPPPP        PGN, 5 hex digits (the usable PGN is in the lowest 18 bits)
    b0..bn       payload, hex digit pairs, concatenated (no spaces)

The Actisense hardware already reassembles fast-packet/multi-packet messages before writing
them out as an ASCII line, so our parser doesn't need to do any CAN frame reassembly.

Since the time of day has no date, the date is guessed from the file name (e.g.
``2026-07-15.raw``), falling back to the file's modification date, with ``--start-date`` as an
explicit override. A midnight rollover within a single file is detected automatically (the
time jumps backwards) and counts as a new day.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Optional, Union

from .model import Frame

_LINE_RE = re.compile(
    r"^A(?P<h>\d{2})(?P<m>\d{2})(?P<s>\d{2})\.(?P<ms>\d{1,3})\s+"
    r"(?P<src>[0-9A-Fa-f]{2})(?P<dst>[0-9A-Fa-f]{2})(?P<prio>[0-9A-Fa-f])\s+"
    r"(?P<pgn>[0-9A-Fa-f]{5})\s+"
    r"(?P<data>[0-9A-Fa-f]+)\s*$"
)

_DATE_IN_NAME_RE = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})")


class _RollingDate:
    """Tracks the current date and advances it on a midnight rollover."""

    def __init__(self, start_date: date) -> None:
        self.current = start_date
        self._last_time_of_day: Optional[timedelta] = None

    def resolve(self, time_of_day: timedelta) -> datetime:
        if self._last_time_of_day is not None and time_of_day < self._last_time_of_day:
            self.current += timedelta(days=1)
        self._last_time_of_day = time_of_day
        return datetime.combine(self.current, datetime.min.time()) + time_of_day


def _guess_start_date(path: Path) -> date:
    match = _DATE_IN_NAME_RE.search(path.stem)
    if match:
        year, month, day = (int(x) for x in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            pass
    return date.fromtimestamp(path.stat().st_mtime)


def _parse_line(line: str, roller: _RollingDate) -> Optional[Frame]:
    match = _LINE_RE.match(line.strip())
    if not match:
        return None

    time_of_day = timedelta(
        hours=int(match["h"]),
        minutes=int(match["m"]),
        seconds=int(match["s"]),
        milliseconds=int(match["ms"].ljust(3, "0")),
    )
    timestamp = roller.resolve(time_of_day)

    pgn = int(match["pgn"], 16) & 0x3FFFF
    try:
        data = bytes.fromhex(match["data"])
    except ValueError:
        return None

    return Frame(
        time=timestamp,
        source=int(match["src"], 16),
        destination=int(match["dst"], 16),
        priority=int(match["prio"], 16),
        pgn=pgn,
        data=data,
    )


def iter_frames_from_lines(lines: Iterable[str], start_date: date) -> Iterator[Frame]:
    """Turns a sequence of text lines (from a file or a live socket stream) into Frames.

    Shared by :func:`iter_frames` (file) and ``network_reader.iter_frames_tcp`` (live TCP
    connection to the W2K-2) so both use the same parsing and midnight-rollover logic.
    """
    roller = _RollingDate(start_date)
    for line in lines:
        if not line.startswith("A"):
            continue
        frame = _parse_line(line, roller)
        if frame is not None:
            yield frame


def iter_frames(path: Union[str, Path], start_date: Optional[date] = None) -> Iterator[Frame]:
    """Read an N2K ASCII log file and yield decoded Frames from it, in file order."""
    path = Path(path)
    resolved_start_date = start_date if start_date is not None else _guess_start_date(path)
    with path.open("r", encoding="ascii", errors="replace") as handle:
        yield from iter_frames_from_lines(handle, resolved_start_date)
