"""Whether the logbook on the phone is still what a build would produce right now.

The apps' Publish button used to assemble the logbook again every time. With this it uploads the logbook as it is, and only
assembles first when it is out of date: a .ebl file newer than the logbook, a changed setting that ends up in it (boat name,
MMSI, call sign, minimum stop time), or newer program code (an app update). Every successful build writes a small file next to
the logbook with what it was built from; ``current_trip_count()`` compares that with the situation now.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Iterable, Optional

SIDECAR_NAME = ".logbook_inputs.json"

# The modules whose code decides what the logbook looks like: a change in one of them (an app update) makes it out of date.
_CODE_MODULES = ("html_writer", "pipeline", "tripbuilder", "translations")


def code_fingerprint() -> str:
    """A hash of the code that builds the logbook; "" for a module whose file cannot be read."""
    digest = hashlib.sha1()
    for name in _CODE_MODULES:
        try:
            spec = importlib.util.find_spec(f"{__package__}.{name}")
            if spec is not None and spec.origin:
                digest.update(Path(spec.origin).read_bytes())
        except (OSError, ImportError, ValueError):
            continue
    return digest.hexdigest()


def inputs(boat_name: str, mmsi: str, call_sign: str, min_stop_minutes: Optional[float]) -> dict:
    """What a build depends on besides the .ebl files."""
    return {
        "boat_name": boat_name,
        "mmsi": mmsi,
        "call_sign": call_sign,
        "min_stop_minutes": None if min_stop_minutes is None else float(min_stop_minutes),
        "code": code_fingerprint(),
    }


def write(html_path: Path, build_inputs: dict, trip_count: int) -> None:
    """Records what the logbook at ``html_path`` was just built from. Best effort: failing to write only means the next
    Publish assembles first."""
    try:
        sidecar = Path(html_path).with_name(SIDECAR_NAME)
        sidecar.write_text(json.dumps({"inputs": build_inputs, "trip_count": trip_count}), encoding="utf-8")
    except OSError:
        pass


def current_trip_count(html_path: str, ebl_paths: Iterable[str], build_inputs: dict) -> Optional[int]:
    """The number of trips of the logbook at ``html_path`` when it is up to date, else None (assemble first)."""
    html = Path(html_path)
    paths = list(ebl_paths)
    if not paths or not html.is_file():
        return None
    try:
        data = json.loads(html.with_name(SIDECAR_NAME).read_text(encoding="utf-8"))
        built_at = html.stat().st_mtime
        if any(Path(p).stat().st_mtime > built_at for p in paths):
            return None
    except (OSError, ValueError):
        return None
    trips = data.get("trip_count") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("inputs") != build_inputs or not isinstance(trips, int):
        return None
    return trips
