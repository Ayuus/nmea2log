"""Reads Actisense EBL log files (SD card log of the W2K-1/W2K-2, BST-95 CAN-raw format).

This format was never officially published by Actisense. The framing (ESC/SOH/NL wrapping
with byte stuffing) and the CAN ID decoding below were taken from the open-source Go
implementation in github.com/aldas/go-nmea-client (actisense/eblreader.go) and verified by
hand against the test vectors in it (including a PGN 129025 example that decodes exactly to
priority=2, pgn=129025, source=0, destination=255).

Important difference from the N2K ASCII path (``ascii_reader.py``):

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

Validated against real SD card logs from a W2K-2 with a Yanmar 4LV195Z engine: a complete cold
engine start (fuel rate, oil pressure build-up, warm-up, hour meter, even the "Preheat
Indicator" warning during preheating) came out physically plausible and internally consistent
(see README).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple, Union

from .model import Frame
from .pgn_decode import PGN_ENGINE_DYNAMIC, PGN_SYSTEM_TIME, PGN_TRIP_FUEL_ENGINE, decode_system_time

_ESC = 0x1B
_SOH = 0x01
_NL = 0x0A
_CMD_RAW_ACTISENSE_MESSAGE_RECEIVED = 0x95

# PGNs that are larger than 8 bytes for us and thus go over the bus as NMEA2000 "Fast Packet".
_FAST_PACKET_PGNS = {PGN_ENGINE_DYNAMIC, PGN_TRIP_FUEL_ENGINE}

_STATE_WAITING = 0
_STATE_READING = 1
_STATE_ESCAPING = 2


def _iter_raw_records(data: bytes) -> Iterator[bytes]:
    """Extracts ESC/SOH/NL-wrapped records from the raw file bytes (with byte stuffing)."""
    state = _STATE_WAITING
    message = bytearray()
    previous_byte: Optional[int] = None

    for current_byte in data:
        if state == _STATE_WAITING:
            if previous_byte == _ESC and current_byte == _SOH:
                state = _STATE_READING
                message = bytearray()
        elif state == _STATE_READING:
            if current_byte == _ESC:
                state = _STATE_ESCAPING
            else:
                message.append(current_byte)
        elif state == _STATE_ESCAPING:
            if current_byte == _ESC:  # double ESC = literal 0x1B data byte
                message.append(current_byte)
                state = _STATE_READING
            elif current_byte == _NL:  # ESC+NL = end of record
                if len(message) > 4:
                    yield bytes(message)
                message = bytearray()
                state = _STATE_WAITING
            else:  # unknown ESC+???  sequence: discard this record, wait for a new start
                message = bytearray()
                state = _STATE_WAITING
        previous_byte = current_byte


def _parse_can_id(can_id: int) -> Tuple[int, int, int, int]:
    """Decomposes a 29-bit extended CAN ID into (priority, pgn, source, destination)."""
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
    """raw = everything after the '07 95' header: length(1) + time counter(2, ignored) + CAN ID(4) + data."""
    if len(raw) < 8:
        return None
    if raw[0] != len(raw) - 1:
        return None  # length field doesn't match -> probably a corrupt record
    can_id = raw[3] | (raw[4] << 8) | (raw[5] << 16) | (raw[6] << 24)
    priority, pgn, source, destination = _parse_can_id(can_id)
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


def iter_frames(
    path: Union[str, Path], time_state: Optional[Dict[str, Optional[datetime]]] = None
) -> Iterator[Frame]:
    """Reads an EBL log file and yields decoded Frames from it, in file order.

    Requires a PGN 126992 (System Time) message to get an absolute time reference; frames
    before that are skipped. ``time_state`` is a mutable dict (key ``"current"``) that tracks
    the last known time; pass the same dict to consecutive files from the same session so the
    time reference is preserved across file boundaries (see the module docstring above).
    Default (``None``) starts each file with a clean slate, as before.
    """
    if time_state is None:
        time_state = {}
    data = Path(path).read_bytes()
    fast_packet_state: Dict[Tuple[int, int], _FastPacketAssembly] = {}

    for record in _iter_raw_records(data):
        if len(record) < 2 or record[0] != 0x07 or record[1] != _CMD_RAW_ACTISENSE_MESSAGE_RECEIVED:
            continue
        decoded = _decode_bst95_record(record[2:])
        if decoded is None:
            continue
        priority, pgn, source, destination, payload = decoded

        if pgn in _FAST_PACKET_PGNS:
            payload = _reassemble_fast_packet((source, pgn), payload, fast_packet_state)
            if payload is None:
                continue

        if pgn == PGN_SYSTEM_TIME:
            decoded_time = decode_system_time(payload)
            if decoded_time is not None:
                time_state["current"] = decoded_time
            continue

        current_time = time_state.get("current")
        if current_time is None:
            continue  # no time reference seen yet (in this file or an earlier one in the session)

        yield Frame(
            time=current_time, source=source, destination=destination, priority=priority, pgn=pgn, data=payload
        )
