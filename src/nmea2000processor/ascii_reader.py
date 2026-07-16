"""Leest Actisense 'N2K ASCII'-logbestanden (het formaat dat de W2K-2 wegschrijft).

Regelformaat (zie Actisense-documentatie 'NMEA 2000 ASCII Output format'):

    Ahhmmss.ddd SSDDP PPPPP b0b1b2...bn

    A            vaste letter, "ontvangen bericht"
    hhmmss.ddd   tijdstip op de dag (géén datum!)
    SS           bronadres, 2 hex-cijfers
    DD           doeladres, 2 hex-cijfers
    P            prioriteit 0-7, 1 hex-cijfer
    PPPPP        PGN, 5 hex-cijfers (de bruikbare PGN zit in de laagste 18 bits)
    b0..bn       payload, hex-cijferparen, aaneengesloten (geen spaties)

De Actisense-hardware zet fast-packet/multi-packet-berichten al in elkaar voordat ze als
ASCII-regel worden weggeschreven, dus onze parser hoeft géén CAN-frame reassemblage te doen.

Omdat het tijdstip geen datum bevat, wordt de datum geraden uit de bestandsnaam (bv.
``2026-07-15.raw``) of anders uit de wijzigingsdatum van het bestand, met ``--start-date``
als expliciete override. Een middernacht-doorgang binnen één bestand wordt automatisch
gedetecteerd (het tijdstip springt terug) en telt als een nieuwe dag.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional, Union

from .model import Frame

_LINE_RE = re.compile(
    r"^A(?P<h>\d{2})(?P<m>\d{2})(?P<s>\d{2})\.(?P<ms>\d{1,3})\s+"
    r"(?P<src>[0-9A-Fa-f]{2})(?P<dst>[0-9A-Fa-f]{2})(?P<prio>[0-9A-Fa-f])\s+"
    r"(?P<pgn>[0-9A-Fa-f]{5})\s+"
    r"(?P<data>[0-9A-Fa-f]+)\s*$"
)

_DATE_IN_NAME_RE = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})")


class _RollingDate:
    """Houdt de actuele datum bij en verhoogt die bij een middernacht-doorgang."""

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


def iter_frames(path: Union[str, Path], start_date: Optional[date] = None) -> Iterator[Frame]:
    """Lees een N2K ASCII-logbestand en geef er gedecodeerde Frame's van terug, in bestandsvolgorde."""
    path = Path(path)
    roller = _RollingDate(start_date if start_date is not None else _guess_start_date(path))
    with path.open("r", encoding="ascii", errors="replace") as handle:
        for line in handle:
            if not line.startswith("A"):
                continue
            frame = _parse_line(line, roller)
            if frame is not None:
                yield frame
