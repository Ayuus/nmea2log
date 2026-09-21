"""Reads Actisense EBL log files (SD card log of the W2K-1/W2K-2, BST-95 CAN-raw format).

This format was never officially published by Actisense. The framing (ESC/SOH/NL wrapping
with byte stuffing) and the CAN ID decoding below were taken from the open-source Go
implementation in github.com/aldas/go-nmea-client (actisense/eblreader.go) and verified by
hand against the test vectors in it (including a PGN 129025 example that decodes exactly to
priority=2, pgn=129025, source=0, destination=255).

Two properties of the format that shape the reader below:

1. EBL contains **raw CAN frames** (max. 8 data bytes), so PGNs larger than 8 bytes (for us:
   127489 and 127497) have to be reassembled ourselves via the NMEA2000 "Fast Packet"
   protocol -- that happens here.
2. Each EBL record does have its own 2-byte time counter, but its meaning is nowhere reliably
   documented (even the reference implementation above only guesses at it, and in practice
   just uses the read time). That counter is therefore ignored here. Instead, the absolute
   date/time is derived from **PGN 126992 (System Time)**, which is already present in the
   N2K stream itself and has a fully documented, unambiguous encoding. Consequence: frames
   before the first 126992 message are skipped (there's no time reference yet at that point),
   and the time resolution equals the transmit rate of PGN 126992 on your NMEA2000 network
   (usually around 1x/second).

   During long periods without GPS/instrument activity (e.g. at anchor, plotter off), an
   entire file can contain no 126992 message at all, while other PGNs (autopilot, gyro) keep
   coming in normally. So when processing multiple consecutive files, pass the same
   ``time_state`` dict to every ``iter_frames`` call (see ``cli.py``): the last known time then
   stays valid across file boundaries, instead of an entire file being silently discarded just
   because it happens not to contain a 126992 message itself.

   More than one device on the bus can send PGN 126992 at once (found in practice: two, whose
   clocks disagreed by ~1s) -- see ``_update_time_state`` for how the reading actually used is
   picked (whichever source has sent the most so far this session), rather than just whichever
   source's message happened to arrive last.

Validated against real SD card logs from a W2K-2 with a Yanmar 4LV195Z engine: a complete cold
engine start (fuel rate, oil pressure build-up, warm-up, hour meter, even the "Preheat
Indicator" warning during preheating) came out physically plausible and internally consistent
(see README).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, Optional, Tuple, Union

from .model import Frame
from .pgn_decode import PGN_ENGINE_DYNAMIC, PGN_SYSTEM_TIME, PGN_TRIP_FUEL_ENGINE, decode_system_time

_ESC = 0x1B
_SOH = 0x01
_NL = 0x0A
_CMD_RAW_ACTISENSE_MESSAGE_RECEIVED = 0x95

# PGNs that are larger than 8 bytes for us and thus go over the bus as NMEA2000 "Fast Packet".
_FAST_PACKET_PGNS = {PGN_ENGINE_DYNAMIC, PGN_TRIP_FUEL_ENGINE}


_ESC_SOH = bytes([_ESC, _SOH])
_ESC_BYTE = bytes([_ESC])


def _iter_raw_records(data: bytes) -> Iterator[bytes]:
    """Extracts ESC/SOH/NL-wrapped records from the raw file bytes (with byte stuffing).

    Same state machine as a byte-by-byte loop would implement, but scans for the next ESC byte
    with ``bytes.find`` (a C-level memory scan) and bulk-copies whole runs of non-ESC bytes via
    slicing, instead of a Python-level loop appending one byte at a time -- real logs run into
    the hundreds of millions of bytes, where the per-byte version dominates total runtime."""
    pos = 0
    n = len(data)
    message = bytearray()

    while pos < n:
        # STATE_WAITING: look for the next ESC+SOH marker (start of a record).
        start = data.find(_ESC_SOH, pos)
        if start == -1:
            return
        pos = start + 2
        message = bytearray()

        # STATE_READING / STATE_ESCAPING: copy bytes up to the next ESC in bulk, then handle
        # the byte right after it (literal ESC, end-of-record, or an unknown/malformed escape).
        while True:
            esc = data.find(_ESC_BYTE, pos)
            if esc == -1:
                return  # ESC (or the whole record) never closes -- nothing left to yield
            message.extend(data[pos:esc])
            if esc + 1 >= n:
                return  # trailing ESC with nothing after it at EOF
            following = data[esc + 1]
            pos = esc + 2
            if following == _ESC:  # double ESC = literal 0x1B data byte
                message.append(_ESC)
                continue
            if following == _NL:  # ESC+NL = end of record
                if len(message) > 4:
                    yield bytes(message)
                break
            break  # unknown ESC+??? sequence: discard this record, wait for a new start


def _parse_can_id(can_id: int) -> Tuple[int, int, int, int]:
    """Decomposes a 29-bit extended CAN ID into (priority, pgn, source, destination). Kept
    around (and covered by its own tests) as the readable reference for the inlined version in
    ``_decode_bst95_record`` -- that hot path (millions of calls per real log) skips the extra
    function-call and tuple pack/unpack overhead of calling this separately."""
    source = can_id & 0xFF
    ps = (can_id >> 8) & 0xFF
    pf = (can_id >> 16) & 0xFF
    dp = (can_id >> 24) & 0x1
    priority = (can_id >> 26) & 0x7
    if pf < 240:  # PDU1: addressed message, the PS byte is the destination address
        pgn = (dp << 16) | (pf << 8)
        destination = ps
    else:  # PDU2: broadcast, the PS byte is part of the PGN
        pgn = (dp << 16) | (pf << 8) | ps
        destination = 0xFF
    return priority, pgn, source, destination


def _decode_bst95_record(raw: bytes) -> Optional[Tuple[int, int, int, int, bytes]]:
    """raw = everything after the '07 95' header: length(1) + time counter(2, ignored) + CAN ID(4) + data.

    Inlines ``_parse_can_id`` (see its docstring) since this runs once per CAN record in the
    whole file -- millions of times for a real multi-day log -- so avoiding the extra call and
    intermediate tuple noticeably adds up."""
    n = len(raw)
    if n < 8 or raw[0] != n - 1:
        return None  # too short, or the length field doesn't match -> probably a corrupt record
    can_id = raw[3] | (raw[4] << 8) | (raw[5] << 16) | (raw[6] << 24)
    source = can_id & 0xFF
    ps = (can_id >> 8) & 0xFF
    pf = (can_id >> 16) & 0xFF
    dp = (can_id >> 24) & 0x1
    priority = (can_id >> 26) & 0x7
    if pf < 240:  # PDU1: addressed message, the PS byte is the destination address
        pgn = (dp << 16) | (pf << 8)
        destination = ps
    else:  # PDU2: broadcast, the PS byte is part of the PGN
        pgn = (dp << 16) | (pf << 8) | ps
        destination = 0xFF
    return priority, pgn, source, destination, raw[7:]


@dataclass
class _FastPacketAssembly:
    seq_counter: int
    total_length: int
    data: bytearray
    next_frame_index: int


def _reassemble_fast_packet(
    key: Tuple[int, int], payload: bytes, state: Dict[Tuple[int, int], _FastPacketAssembly]
) -> Optional[bytes]:
    """NMEA2000 Fast Packet reassembly: byte 0 = (sequence-counter<<5 | frame-index)."""
    if len(payload) < 2:
        return None
    frame_header = payload[0]
    seq_counter = frame_header >> 5
    frame_index = frame_header & 0x1F

    if frame_index == 0:
        total_length = payload[1]
        assembly = _FastPacketAssembly(seq_counter, total_length, bytearray(payload[2:8]), 1)
        state[key] = assembly
    else:
        assembly = state.get(key)
        if assembly is None or assembly.seq_counter != seq_counter or assembly.next_frame_index != frame_index:
            state.pop(key, None)  # missed or unexpected frame -> give up this reassembly
            return None
        assembly.data.extend(payload[1:8])
        assembly.next_frame_index += 1

    if len(assembly.data) >= assembly.total_length:
        state.pop(key, None)
        return bytes(assembly.data[: assembly.total_length])
    return None


def _update_time_state(time_state: Dict[str, object], source: int, decoded_time: datetime) -> None:
    """Updates ``time_state["current"]`` from a newly-decoded PGN 126992 (System Time) reading,
    but only if ``source`` is (or has just become) the most prevalent source of these readings
    seen so far this session -- not unconditionally, the way this used to just take whichever
    source's message happened to arrive last.

    Found in practice, on a real boat: more than one device on the N2K bus can send PGN 126992 at
    once (here: two, one second apart from each other) -- this PGN carries no field distinguishing
    a GPS-verified reading from a device's own less-authoritative clock, so there's no way to tell
    which one is "right" from a single message alone. Blindly taking whichever one arrived last
    made the decoded time step backward by ~1s every time the two sources' messages happened to
    interleave -- confirmed to be the direct cause of small backward-jump violations later found
    in the season-wide fixes/sogs/attitude arrays (see fix_array.py's own drop_time_regressions methods).
    Cumulative message counts, kept in ``time_state`` itself (so they carry over between files the
    same way ``time_state["current"]`` already does), converge on the same choice
    _select_primary_gps_source() already makes for position fixes from the same kind of
    multi-source ambiguity -- "whichever source sends the most" -- computed online instead of
    needing a second pass over the whole archive first to find that answer."""
    counts = time_state.setdefault("source_counts", {})
    counts[source] = counts.get(source, 0) + 1
    preferred_source = time_state.get("time_source")
    # Strictly more, not "at least as many": with two sources sending at almost the same rate
    # (found in practice: 1456 vs 1446 messages) a tie must keep the current choice -- switching
    # on every tie made the decoded time hop between the two clocks whenever the counts happened
    # to be level, e.g. right after a decode that restarted with its counts at zero.
    if preferred_source is None or counts[source] > counts.get(preferred_source, 0):
        time_state["time_source"] = source
        time_state["current"] = decoded_time
    elif source == preferred_source:
        time_state["current"] = decoded_time


# Everything in ``time_state`` that decides the time of later frames -- not just the last time
# itself: which source is trusted (``time_source``) follows from the cumulative ``source_counts``,
# so a decode that continues from a cache (sample_cache.py, trip_cache.py) must get all of it back,
# otherwise it starts over with empty counts and can trust a different clock than an uninterrupted
# decode of the very same files would (found in practice: the same file decoded on a phone that had
# resumed from its cache and on a tablet that hadn't differed by 1-3 s in ~40% of its rows).
_PERSISTED_TIME_STATE_KEYS = ("current", "time_source", "source_counts")


def snapshot_time_state(time_state: Dict[str, object]) -> Dict[str, object]:
    """A self-contained copy of the parts of ``time_state`` that decide later timestamps, safe to
    keep (and pickle) while the live dict goes on changing."""
    snapshot = {key: time_state[key] for key in _PERSISTED_TIME_STATE_KEYS if key in time_state}
    if "source_counts" in snapshot:
        snapshot["source_counts"] = dict(snapshot["source_counts"])  # type: ignore[arg-type]
    return snapshot


def restore_time_state(time_state: Dict[str, object], snapshot: object) -> None:
    """Puts ``time_state`` back exactly as it was when ``snapshot_time_state`` took ``snapshot``.
    A bare datetime (what older caches stored: only the last time) or None restores just that."""
    time_state.clear()
    if isinstance(snapshot, datetime):
        time_state["current"] = snapshot
    elif isinstance(snapshot, dict):
        time_state.update(snapshot)
        if "source_counts" in snapshot:
            time_state["source_counts"] = dict(snapshot["source_counts"])


def iter_frames(
    path: Union[str, Path],
    time_state: Optional[Dict[str, object]] = None,
    wanted_pgns: Optional[FrozenSet[int]] = None,
) -> Iterator[Frame]:
    """Reads an EBL log file and yields decoded Frames from it, in file order.

    Requires a PGN 126992 (System Time) message to get an absolute time reference; frames
    before that are skipped. ``time_state`` is a mutable dict (key ``"current"``, plus
    ``"source_counts"``/``"time_source"`` -- see ``_update_time_state``) that tracks the last
    known time; pass the same dict to consecutive files from the same session so the time
    reference (and each source's own running message count) is preserved across file boundaries
    (see the module docstring above). Default (``None``) starts each file with a clean slate, as
    before.

    ``wanted_pgns``: if given, frames whose PGN isn't in this set are dropped right after
    decoding the CAN ID, before the (comparatively expensive) Fast Packet reassembly and Frame
    construction -- PGN 126992 always passes through regardless, since it's needed internally
    for the time reference above. Real NMEA2000 buses carry a lot of chatter this app has no use
    for (autopilot/heading/attitude PGNs can easily outnumber the ones it decodes 100:1); most
    real logs are effectively this filter's cost, not the file-reading cost.
    """
    if time_state is None:
        time_state = {}
    data = Path(path).read_bytes()
    fast_packet_state: Dict[Tuple[int, int], _FastPacketAssembly] = {}

    for record in _iter_raw_records(data):
        # _iter_raw_records only ever yields records with len(message) > 4 (see its "> 4" check),
        # so record is always at least 5 bytes here -- no need to re-check the length.
        if record[0] != 0x07 or record[1] != _CMD_RAW_ACTISENSE_MESSAGE_RECEIVED:
            continue
        decoded = _decode_bst95_record(record[2:])
        if decoded is None:
            continue
        priority, pgn, source, destination, payload = decoded

        if wanted_pgns is not None and pgn not in wanted_pgns and pgn != PGN_SYSTEM_TIME:
            continue

        if pgn in _FAST_PACKET_PGNS:
            payload = _reassemble_fast_packet((source, pgn), payload, fast_packet_state)
            if payload is None:
                continue

        if pgn == PGN_SYSTEM_TIME:
            decoded_time = decode_system_time(payload)
            if decoded_time is not None:
                _update_time_state(time_state, source, decoded_time)
            continue

        current_time = time_state.get("current")
        if current_time is None:
            continue  # no time reference seen yet (in this file or an earlier one in the session)

        yield Frame(
            time=current_time, source=source, destination=destination, priority=priority, pgn=pgn, data=payload
        )
