"""Assigns each trip a stable identifier that survives future changes to the trip-building
algorithm (e.g. a fix that shifts a trip's exact departure/arrival time by a few minutes, like
the gap-splitting fix in tripbuilder.py did) -- matching is on local date + depart/arrive
position, not the exact timestamp, so that kind of fix doesn't change the id.

Deterministic (a uuid5 hash of that same date+position key), not random: an earlier version
generated a random uuid4 the first time a trip was seen and persisted it in a registry file
(trip_ids.json) so the same trip could look it up again on a later run. That meant two overlapping
runs against the same project directory (found in practice: manual testing overlapping with a
real run) could each independently decide on their own random id for a trip neither had seen yet,
and whichever run's write happened last silently won -- orphaning the other run's id, and with it
any remark already saved against it. Deriving the id straight from the trip's own stable key
removes the registry (and that whole race) entirely: the same trip always hashes to the same id,
on any machine, with no state to keep in sync.
"""

from __future__ import annotations

import uuid
from typing import Dict, List, Optional

from .logbook_writer import _to_local, _trip_utc_offset_hours
from .tripbuilder import TripLeg

# Fixed, arbitrary namespace for uuid5 -- only needs to be constant across runs (so the same
# match key always hashes to the same uuid), not meaningful on its own.
_NAMESPACE = uuid.UUID("c6f6f5c0-2b8a-4b7a-9b2a-6f5c0c6f6f5c")


def _position_key(trip: TripLeg) -> str:
    """Rounded depart/arrive GPS coordinates, not the *displayed* place name -- found in
    practice: matching on ``depart_place``/``arrive_place`` text broke as soon as that text
    changed for any reason unrelated to the trip itself (geocoding on vs. off, a future
    geocoding fix returning a different name for the same spot, --language, ...), silently
    orphaning that trip's id -- and with it, any remark already saved against the old one.
    Rounded to ~100 m (3 decimals), which comfortably groups GPS noise around the same port
    without conflating two actually-different nearby locations. Falls back to the place-name
    text only if there's no track at all to take a position from."""
    if not trip.track:
        return f"{trip.depart_place}|{trip.arrive_place}"
    depart, arrive = trip.track[0], trip.track[-1]
    return f"{depart.lat:.3f},{depart.lon:.3f}|{arrive.lat:.3f},{arrive.lon:.3f}"


def _match_key(trip: TripLeg, utc_offset_hours: Optional[float], occurrence: int) -> str:
    """A trip's identity: local departure date + depart/arrive position, plus an occurrence
    counter to tell apart repeated same-day round trips between the same two spots. Deliberately
    doesn't include the exact time -- that's the whole point, since exact times are exactly what
    a trip-recognition fix might shift."""
    offset = _trip_utc_offset_hours(trip, utc_offset_hours)
    local_date = _to_local(trip.depart_time, offset).date().isoformat()
    return f"{local_date}|{_position_key(trip)}|{occurrence}"


def assign_trip_ids(trips: List[TripLeg], utc_offset_hours: Optional[float] = None) -> List[str]:
    """Returns one stable id per trip, in the same order as ``trips`` -- a uuid5 hash of each
    trip's own match key (see ``_match_key``), so the same trip always gets the same id, on any
    machine, with nothing to persist or keep in sync."""
    occurrence_counts: Dict[str, int] = {}
    uids: List[str] = []
    for trip in trips:
        base_key = _match_key(trip, utc_offset_hours, 0).rsplit("|", 1)[0]
        occurrence = occurrence_counts.get(base_key, 0)
        occurrence_counts[base_key] = occurrence + 1
        key = f"{base_key}|{occurrence}"
        uids.append(str(uuid.uuid5(_NAMESPACE, key)))
    return uids
