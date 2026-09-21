"""Keeps a GNSS receiver's position frames out of the data while the receiver itself says it has
no valid fix.

Why: position frames (PGN 129025) keep coming even while a receiver has no valid solution --
confirmed on a real boat: in the last second before it lost power the final fix jumped 173 m (fix
mode and HDOP both "not available"), and at the next power-on the receiver started from that same
wrong position (HDOP 99) and only converged on the real one ~100 s later. The receiver states this
itself in PGN 129539 (GNSS DOPs), sent every ~2 s, so that is what gates the positions here.
"""

from __future__ import annotations

import sys
from typing import Dict, List, Optional, Set

from .log import log
from .model import PositionFix
from .stretches import StretchLog, format_stretch

# canboat's GNSS_MODE values that mean the receiver has a position solution (1D/2D/3D/Auto);
# anything else (6 = Error, or "not available") means it doesn't.
_GNSS_FIX_MODES = frozenset({0, 1, 2, 3})

# HDOP a receiver reports as a placeholder while it has no satellites yet -- 99.00 on the real boat
# above, for the first seconds after power-on.
_NO_FIX_HDOP = 50.0


def gnss_fix_usable(actual_mode: Optional[int], hdop: Optional[float], ever_reported_valid: bool) -> bool:
    """Whether a receiver's own DOP message says its position frames can be trusted right now.

    "Nothing available" only counts as a lost fix for a receiver that *has* reported a real
    solution before (``ever_reported_valid``) -- a device that never fills these fields in at all
    must not have every one of its positions thrown away."""
    if hdop is not None and hdop >= _NO_FIX_HDOP:
        return False
    if actual_mode in _GNSS_FIX_MODES:
        return True
    if actual_mode is None and hdop is None:
        return not ever_reported_valid
    return False  # an explicit error/reserved mode


class GnssFixGate:
    """Collects position fixes per source, accepting one only if the DOP messages on *both* sides
    of it say the receiver had a fix. Positions arrive several times per second but DOP messages
    only every ~2 s, and the first wrong positions after a power-on (or the last ones before a
    power-off) come before the DOP message that says so -- so a fix is held back until the next
    DOP message shows up, instead of being judged only by the one before it.

    A source that has sent no DOP message at all (yet) counts as usable: only positive evidence of
    a lost fix discards a position. State is per instance, and an instance covers one file (like
    everything else in pipeline.py's _collect_samples, see sample_cache.py)."""

    def __init__(self) -> None:
        self.accepted: Dict[int, List[PositionFix]] = {}
        # Per source, the stretches of ignored fixes; kept as separate stretches (not one total)
        # so the log says *when*: a shutdown and the start-up hours later in the same file are two
        # different events.
        self.ignored: Dict[int, StretchLog] = {}
        self._usable: Dict[int, bool] = {}
        self._valid_seen: Set[int] = set()
        self._pending: Dict[int, List[PositionFix]] = {}

    def add_fix(self, source: int, fix: PositionFix) -> None:
        self._pending.setdefault(source, []).append(fix)

    def on_dop(self, source: int, actual_mode: Optional[int], hdop: Optional[float]) -> None:
        usable = gnss_fix_usable(actual_mode, hdop, source in self._valid_seen)
        self._settle(source, self._usable.get(source, True) and usable)
        self._usable[source] = usable
        if usable and (actual_mode is not None or hdop is not None):
            self._valid_seen.add(source)

    def finish(self) -> None:
        """Settles whatever arrived after each source's last DOP message (judged by that DOP
        message alone), and reports how many fixes were ignored in total."""
        for source in list(self._pending):
            self._settle(source, self._usable.get(source, True))
        for source, ignored in self.ignored.items():
            for stretch in ignored.stretches:
                log(
                    f"[info] Ignored {stretch.count} position fix(es) from source {source} at "
                    f"{format_stretch(stretch, with_count=False)} while its "
                    f"GNSS receiver reported no valid fix (start-up or shutdown -- it keeps sending its "
                    f"last remembered position meanwhile).",
                    file=sys.stderr,
                )

    def _settle(self, source: int, accept: bool) -> None:
        pending = self._pending.pop(source, [])
        if accept:
            self.accepted.setdefault(source, []).extend(pending)
        elif pending:
            # One settle can span a power cycle (no DOP message while the receiver was off, so the
            # last fixes before shutdown and the first after start-up are settled together), so the
            # split into stretches is by the fixes' own times, not by the call.
            ignored = self.ignored.setdefault(source, StretchLog())
            for fix in pending:
                ignored.add(fix.time)
