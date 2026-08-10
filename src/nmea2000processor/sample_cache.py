"""Caches decoded samples per .ebl file, keyed on file size, so a normal day-to-day run doesn't
have to re-parse the whole season's worth of log files every time -- only the file(s) that are
new or have grown since the last run. A real multi-day log easily has hundreds of files, and in
practice only the newest one or two ever change between runs.

Not used for the N2K ASCII path (.raw/.n2k, --live --tee): those aren't the "hundreds of files"
problem this exists for, and their decoding also depends on --start-date, which complicates
caching for no real benefit there.
"""

from __future__ import annotations

import pickle
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Bump this whenever the decoded sample format changes (new PGN, changed decode logic, new field
# on one of the sample dataclasses, ...) so a cache written by older code doesn't silently keep
# being served after an upgrade that should have changed its contents -- the whole cache is
# discarded and rebuilt from scratch when the stored version doesn't match.
CACHE_FORMAT_VERSION = 1

# Raw pickled samples are highly repetitive (many similar-shaped dataclass instances), so zlib
# compresses them roughly 10x for very little time cost -- measured on a real 625-file, ~500 MB
# cache: ~3s to compress, ~0.4s to decompress, down to ~45 MB on disk.
_COMPRESSION_LEVEL = 6


def _cache_key(path: Path) -> str:
    """Resolves to an absolute, canonical path before using it as a cache key -- otherwise
    running the same command with a relative vs. an absolute --ebl-dir (or from a different
    working directory) produces a different string for the exact same file, silently missing
    the cache every time (found in practice)."""
    return str(path.resolve())


class SampleCache:
    def __init__(self, cache_file: Path) -> None:
        self.cache_file = cache_file
        self._entries: Dict[str, dict] = {}
        self._dirty = False
        if cache_file.exists():
            try:
                data = pickle.loads(zlib.decompress(cache_file.read_bytes()))
                if data.get("version") == CACHE_FORMAT_VERSION:
                    self._entries = data.get("files", {})
            except Exception:
                # Corrupt, truncated, or otherwise unreadable cache file -- treat it the same as
                # "no cache yet" rather than crashing the whole run over a perf optimization.
                self._entries = {}

    def get(self, path: Path) -> Optional[Tuple[tuple, Optional[datetime]]]:
        """Returns (samples, time_state_after) if this exact file (by size) is cached, else
        None. Only the size is checked (not mtime): the file's own content is what we actually
        care about, and re-downloading the same log can easily change the mtime without changing
        a single byte of content."""
        entry = self._entries.get(_cache_key(path))
        if entry is None or entry.get("size") != path.stat().st_size:
            return None
        return entry["samples"], entry.get("time_state_after")

    def put(self, path: Path, samples: tuple, time_state_after: Optional[datetime]) -> None:
        self._entries[_cache_key(path)] = {
            "size": path.stat().st_size,
            "samples": samples,
            "time_state_after": time_state_after,
        }
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        payload: Dict[str, Any] = {"version": CACHE_FORMAT_VERSION, "files": self._entries}
        compressed = zlib.compress(pickle.dumps(payload), level=_COMPRESSION_LEVEL)
        self.cache_file.write_bytes(compressed)
