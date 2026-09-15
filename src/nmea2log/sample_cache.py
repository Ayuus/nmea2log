"""Caches decoded samples per .ebl file, keyed on file size, so a normal day-to-day run doesn't
have to re-parse the whole season's worth of log files every time -- only the file(s) that are
new or have grown since the last run.

One small file per source .ebl file on disk (see _entry_path), not one big combined pickle --
found in practice on a real ~2000-file, multi-day archive: the old single-file format had to load
every cached entry into memory up front just to look up the *one* file the current step needed,
even though the vast majority weren't touched again this run. On a phone that peak (measured:
~500 MB decompressed for 625 files, so easily 1+ GB for a full season) stacked on top of the
samples already merged in from files processed earlier in the same run was enough to get the
whole process killed by Android's low-memory killer partway through -- losing everything not
already flushed to disk. Reading and writing one file's own entry at a time keeps the cache's own
footprint at "whatever the current file needs" regardless of how many others exist.
"""

from __future__ import annotations

import dataclasses
import hashlib
import pickle
import shutil
import zlib
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from .log import log

# Bump this whenever the decoded sample format changes (new PGN, changed decode logic, new field
# on one of the sample dataclasses, ...) so a cache written by older code doesn't silently keep
# being served after an upgrade that should have changed its contents -- every entry stamped with
# an older/newer version is treated as absent and simply gets overwritten the next time that file
# is put() again.
CACHE_FORMAT_VERSION = 3

# Raw pickled samples are highly repetitive (many similar-shaped dataclass instances), so zlib
# compresses them roughly 10x for very little time cost.
_COMPRESSION_LEVEL = 6


def _encode_samples(samples: list) -> Optional[tuple]:
    """Turns a list of dataclass instances into a columnar form: one tuple of values per field,
    instead of one pickled object per sample. A single file can hold thousands of tiny dataclass
    instances (position fixes, attitude samples, ...), and unpickling each one individually
    (per-object REDUCE/BUILD) is what actually costs the time -- pickling a handful of big,
    homogeneous tuples of plain values instead cuts that roughly in half. Returns None for an
    empty list so the common "this PGN wasn't in this file" case doesn't store an empty
    tuple-of-tuples."""
    if not samples:
        return None
    cls = type(samples[0])
    field_names = tuple(f.name for f in dataclasses.fields(cls))
    columns = tuple(tuple(getattr(sample, name) for sample in samples) for name in field_names)
    return cls, columns


def _decode_samples(encoded: Optional[tuple]) -> list:
    if encoded is None:
        return []
    cls, columns = encoded
    return [cls(*row) for row in zip(*columns)]


def _encode_samples_tuple(samples: tuple) -> tuple:
    """Encodes one file's full 9-part samples tuple from _collect_samples (see cli.py): some
    parts are a flat list (engine, trip fuel, RPM), others a dict keyed by NMEA source address
    (position, SOG, depth, water temp, battery, attitude) -- told apart by the value's own type
    rather than its position, so this doesn't need updating if that tuple's shape ever changes."""
    return tuple(
        {source: _encode_samples(items) for source, items in value.items()}
        if isinstance(value, dict)
        else _encode_samples(value)
        for value in samples
    )


def _decode_samples_tuple(encoded: tuple) -> tuple:
    return tuple(
        {source: _decode_samples(item) for source, item in value.items()}
        if isinstance(value, dict)
        else _decode_samples(value)
        for value in encoded
    )


def _cache_key(path: Path) -> str:
    """Resolves to an absolute, canonical path before using it as a cache key -- otherwise
    running the same command with a relative vs. an absolute --ebl-dir (or from a different
    working directory) produces a different string for the exact same file, silently missing
    the cache every time (found in practice)."""
    return str(path.resolve())


def _entry_path(cache_dir: Path, key: str) -> Path:
    # A hash rather than some sanitized version of the path itself -- path separators, drive
    # letters (Windows desktop vs. Android's own filesystem) and length limits all make a raw
    # path an unreliable filename, while a hash of it is always a fixed-length, filesystem-safe
    # name and collisions are not a practical concern (SHA-1 over a handful of thousand keys).
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.pkl.zz"


def _migrate_legacy_cache(cache_dir: Path) -> None:
    """One-time upgrade from the old single-big-pickle cache (a plain file at this exact path) to
    this one-file-per-source-file directory layout. Reads the legacy file exactly once -- the
    same whole-cache-in-memory cost every run used to pay, just this one last time -- then never
    again. Entries are written to a temporary directory first and only swapped into place right
    at the end, so a crash partway through (this is, ironically, the same kind of memory-pressure
    situation this whole change exists to avoid) leaves the original legacy file untouched for a
    later run to retry the migration from, instead of a half-migrated cache. A no-op once the
    migration has already happened (cache_dir is a real directory by then, not a file). Always
    called before the directory is touched any other way -- a no-op is two cheap existence checks
    when there's nothing to migrate."""
    tmp_dir = cache_dir.with_name(cache_dir.name + ".migrating")
    if tmp_dir.exists():
        # Leftover from an attempt that didn't reach the final swap below (interrupted, or the
        # process was killed) -- discard rather than trying to salvage it. The legacy file (if
        # this crashed before ever unlinking it) or the lack of one either way leaves the next
        # step below to redo the migration correctly from scratch.
        shutil.rmtree(tmp_dir)

    if not cache_dir.is_file():
        return  # nothing to migrate: never existed, or a previous run already finished this

    try:
        data = pickle.loads(zlib.decompress(cache_dir.read_bytes()))
    except Exception:
        # Corrupt or unreadable -- nothing worth migrating, treat as "no cache yet".
        cache_dir.unlink()
        return
    entries = data.get("files", {}) if data.get("version") == CACHE_FORMAT_VERSION else {}
    del data  # the whole-cache-in-memory cost this migration exists to get rid of -- one last time

    if entries:
        # Silent otherwise: this step (unlike everything else in this module) still has to hold
        # the *old* format's whole decompressed cache in memory at once, and on a real multi-day
        # archive that can take a real moment -- found in practice: it looked like decoding had
        # silently stalled right after downloading finished, with nothing on screen to say
        # otherwise (this app's very first run against an existing cache is the one time this
        # runs at all; every run after it is an instant no-op, see the early return above).
        log(f"[info] upgrading sample cache format ({len(entries)} file(s) already decoded)...")
    tmp_dir.mkdir(parents=True)
    for key, entry in entries.items():
        payload = zlib.compress(
            pickle.dumps({**entry, "version": CACHE_FORMAT_VERSION}, protocol=pickle.HIGHEST_PROTOCOL),
            level=_COMPRESSION_LEVEL,
        )
        _entry_path(tmp_dir, key).write_bytes(payload)

    cache_dir.unlink()
    tmp_dir.rename(cache_dir)


class SampleCache:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        _migrate_legacy_cache(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, path: Path) -> Optional[Tuple[tuple, Optional[datetime]]]:
        """Returns (samples, time_state_after) if this exact file (by size) is cached, else
        None. Only the size is checked (not mtime): the file's own content is what we actually
        care about, and re-downloading the same log can easily change the mtime without changing
        a single byte of content."""
        entry_path = _entry_path(self.cache_dir, _cache_key(path))
        if not entry_path.exists():
            return None
        try:
            entry = pickle.loads(zlib.decompress(entry_path.read_bytes()))
        except Exception:
            # Corrupt or truncated (e.g. the write itself got interrupted) -- treat exactly like
            # a cache miss rather than crashing the whole run over a perf optimization.
            return None
        if entry.get("version") != CACHE_FORMAT_VERSION or entry.get("size") != path.stat().st_size:
            return None
        return _decode_samples_tuple(entry["samples"]), entry.get("time_state_after")

    def put(self, path: Path, samples: tuple, time_state_after: Optional[datetime]) -> None:
        """Written immediately -- unlike the old whole-cache format, there is no separate save()
        step and nothing to lose if the process is killed before the run finishes: every file's
        own entry is durable on disk the moment it's decoded."""
        entry = {
            "version": CACHE_FORMAT_VERSION,
            "size": path.stat().st_size,
            "samples": _encode_samples_tuple(samples),
            "time_state_after": time_state_after,
        }
        payload = zlib.compress(
            pickle.dumps(entry, protocol=pickle.HIGHEST_PROTOCOL), level=_COMPRESSION_LEVEL
        )
        _entry_path(self.cache_dir, _cache_key(path)).write_bytes(payload)
