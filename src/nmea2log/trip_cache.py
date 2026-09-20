"""Caches fully-built, "settled" trips across runs, so a normal day-to-day sync only has to
decode and process a recent window of .ebl files instead of the entire multi-year archive every
time -- a settled trip never changes again, so re-decoding and re-classifying it every single run
is pure waste (found in practice: this is the single biggest cost behind both a season-wide run's
decode time and its peak memory, see the checkpoint logging around build_trips() in cli.py, and
the ~3.7 GB peak RSS this exists to cut down on the Android app).

Deliberately keeps the *last* known trip out of the cache and always rebuilds it fresh from raw
samples, even across runs where nothing about it looks like it changed -- its own classification
can depend on samples that only arrive *after* it (see _settled_position()/_reclassify_locks() in
tripbuilder.py: whether a stop was "still gliding to a halt" or "genuinely arrived", or whether it
should merge with the next stop, both need a look at what happens right after it). Trusting a trip
as final the moment it's first seen would silently reintroduce exactly the kind of subtly-wrong
trip data this app has already been bitten by in practice -- so every run always re-decodes and
rebuilds at least the whole window starting at the last known trip's own departure.
"""

from __future__ import annotations

import hashlib
import pickle
import zlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .tripbuilder import TripLeg

# Bump whenever the cached payload's shape changes (TripLeg gains/loses a field, etc.) so a cache
# written by older code doesn't silently get served to code that no longer agrees with its shape
# -- an unreadable/mismatched cache is simply treated as absent (see TripCache.load), never as an
# error, since it costs nothing but one slower run to rebuild it from scratch.
CACHE_FORMAT_VERSION = 1

_COMPRESSION_LEVEL = 6


def config_signature(**params: object) -> str:
    """A short hash of every build_trips() parameter (plus anything else that changes what a
    "settled" trip looks like once cached, e.g. the geocoding language) that the cached trips
    were built with. Compared against the current run's own parameters before trusting the cache
    at all -- a changed threshold (e.g. a different --speed-threshold-kn) can change how the same
    raw data would now be classified, and silently keeping stale trips built under old parameters
    would disagree with what a fresh run over the same data would produce (exactly the kind of
    masked inconsistency this app avoids elsewhere)."""
    parts = "|".join(f"{key}={params[key]!r}" for key in sorted(params))
    return hashlib.sha1(parts.encode("utf-8")).hexdigest()


def _resolved(path: Path) -> str:
    return str(path.resolve())


def find_resume_index(logfiles: Sequence[Path], resume_from_file: str) -> Optional[int]:
    """Index into ``logfiles`` matching the file a cached run recorded as its resume point, or
    None if it can't be found (the file was moved, deleted, or --ebl-dir now points somewhere else
    entirely) -- the caller must then treat the whole cache as unusable rather than guess which
    files it actually covers."""
    for index, path in enumerate(logfiles):
        if _resolved(path) == resume_from_file:
            return index
    return None


def choose_resume_index(
    file_first_times: Sequence[Tuple[int, Optional[datetime]]],
    resume_reference_time: datetime,
    min_index: int,
) -> int:
    """Picks which file the *next* run should resume fully decoding from: the last file (by
    index) that starts at or before ``resume_reference_time`` -- the moment the stay right before
    the still-open last known trip's own departure began (its caller passes the *arrival* time of
    the trip immediately before it, i.e. the start of that shared stay -- see the call site in
    cli.py). Deliberately does NOT back up any further "for safety": a stay that happens to be
    split across a file boundary is already handled correctly by the ``<=`` comparison itself
    (an earlier file whose own data starts before the stay began is picked instead), and backing
    up into a file that's *entirely* still part of the previous, already-cached trip's own moving
    leg reintroduces that trip a second time (found in practice: build_trips() has no way to tell
    "this window's opening moving-then-arriving pattern is a genuinely new trip" from "this is
    just the tail end of a trip that's already sitting in settled_trips" -- both look identical
    from inside the reprocessed window alone). Never returns an index earlier than ``min_index``
    (the earliest file this run actually had available to pick from): that just means resuming
    from the same file as this run did, always still correct, only less of a saving next time."""
    candidate: Optional[int] = None
    for index, first_time in file_first_times:
        if first_time is not None and first_time <= resume_reference_time:
            candidate = index
    if candidate is None:
        return min_index
    return max(min_index, candidate)


class TripCache:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(
        self, expected_signature: str
    ) -> Optional[Tuple[List[TripLeg], str, object]]:
        """Returns (settled_trips, resume_from_file, resume_ebl_time_state) if a usable cache
        exists, else None. A missing file, a corrupt/truncated file, a format-version mismatch,
        or a config signature mismatch (see config_signature()) are all treated identically as
        "no usable cache" -- falling back to a full from-scratch run costs time, but risking
        silently wrong trip data from a cache built under different settings does not."""
        if not self.path.exists():
            return None
        try:
            data = pickle.loads(zlib.decompress(self.path.read_bytes()))
        except Exception:
            return None
        if data.get("version") != CACHE_FORMAT_VERSION:
            return None
        if data.get("config_signature") != expected_signature:
            return None
        settled_trips = data.get("settled_trips")
        resume_from_file = data.get("resume_from_file")
        if not settled_trips or not resume_from_file:
            return None
        return settled_trips, resume_from_file, data.get("resume_ebl_time_state")

    def save(
        self,
        settled_trips: List[TripLeg],
        resume_from_file: str,
        resume_ebl_time_state: object,
        config_signature_value: str,
    ) -> None:
        data: Dict[str, object] = {
            "version": CACHE_FORMAT_VERSION,
            "config_signature": config_signature_value,
            "settled_trips": settled_trips,
            "resume_from_file": resume_from_file,
            "resume_ebl_time_state": resume_ebl_time_state,
        }
        payload = zlib.compress(
            pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL), level=_COMPRESSION_LEVEL
        )
        self.path.write_bytes(payload)
