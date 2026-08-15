"""Assigns each trip a stable, random identifier that survives future changes to the trip-
building algorithm (e.g. a fix that shifts a trip's exact departure/arrival time by a few
minutes, like the gap-splitting fix in tripbuilder.py did) -- unlike an id derived from the
trip's own data, a random UUID never changes once assigned to a trip. It's persisted in
``trip_ids.json`` and looked up again on every run by matching a newly-built trip against
previously-seen ones.

This is groundwork for a future "add remarks to a trip" feature: remarks would key off this uid
instead of a timestamp, so they survive routine trip-recognition improvements. Not used for
anything else yet -- the CLI doesn't wire this in yet either.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from .logbook_writer import _to_local, _trip_utc_offset_hours
from .tripbuilder import TripLeg

DEFAULT_REGISTRY_PATH = Path("trip_ids.json")


def _position_key(trip: TripLeg) -> str:
    """Rounded depart/arrive GPS coordinates, not the *displayed* place name -- found in
    practice: matching on ``depart_place``/``arrive_place`` text broke as soon as that text
    changed for any reason unrelated to the trip itself (geocoding on vs. off, a future
    geocoding fix returning a different name for the same spot, --language, ...), silently
    orphaning that trip's uid -- and with it, any remark already saved against the old one.
    Rounded to ~100 m (3 decimals), which comfortably groups GPS noise around the same port
    without conflating two actually-different nearby locations. Falls back to the place-name
    text only if there's no track at all to take a position from."""
    if not trip.track:
        return f"{trip.depart_place}|{trip.arrive_place}"
    depart, arrive = trip.track[0], trip.track[-1]
    return f"{depart.lat:.3f},{depart.lon:.3f}|{arrive.lat:.3f},{arrive.lon:.3f}"


def _match_key(trip: TripLeg, utc_offset_hours: Optional[float], occurrence: int) -> str:
    """A trip's identity for matching purposes: local departure date + depart/arrive position,
    plus an occurrence counter to tell apart repeated same-day round trips between the same two
    spots. Deliberately doesn't include the exact time -- that's the whole point, since exact
    times are exactly what a trip-recognition fix might shift."""
    offset = _trip_utc_offset_hours(trip, utc_offset_hours)
    local_date = _to_local(trip.depart_time, offset).date().isoformat()
    return f"{local_date}|{_position_key(trip)}|{occurrence}"


def assign_trip_ids(
    trips: List[TripLeg],
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    utc_offset_hours: Optional[float] = None,
) -> List[str]:
    """Returns one uuid per trip, in the same order as ``trips``. Reuses a previously-assigned
    uuid whenever a trip matches one already in the registry (see ``_match_key``); generates and
    persists a fresh random uuid otherwise. Writes the (possibly updated) registry back to
    ``registry_path`` before returning."""
    registry: Dict[str, str] = {}
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))

    occurrence_counts: Dict[str, int] = {}
    uids: List[str] = []
    for trip in trips:
        base_key = _match_key(trip, utc_offset_hours, 0).rsplit("|", 1)[0]
        occurrence = occurrence_counts.get(base_key, 0)
        occurrence_counts[base_key] = occurrence + 1
        key = f"{base_key}|{occurrence}"

        trip_uid = registry.get(key)
        if trip_uid is None:
            trip_uid = str(uuid.uuid4())
            registry[key] = trip_uid
        uids.append(trip_uid)

    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    return uids
