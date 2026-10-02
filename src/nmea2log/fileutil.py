"""Small file helpers shared by the on-disk caches."""

from __future__ import annotations

import os
import threading
from pathlib import Path


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    """Writes ``payload`` to ``path`` so a reader never sees half of it: into a temporary file in
    the same directory first, then moved over ``path`` in one step (os.replace is atomic on the
    same filesystem). A plain write_bytes() truncates the target first, so a run killed mid-write
    (the Android app's low-memory killer, a force-stop) or a second run reading the same entry at
    that moment saw a torn file. The temporary name carries the process and thread, so two
    writers of the same entry never share one either."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
