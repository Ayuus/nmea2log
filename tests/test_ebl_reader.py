import struct
from datetime import date, datetime, timedelta
from pathlib import Path

from nmea2000processor.ebl_reader import iter_frames

_ESC = 0x1B
_SOH = 0x01
_NL = 0x0A


def _frame_bytes(message: bytes) -> bytes:
    """Omkadert een record met ESC/SOH...ESC/NL en past byte-stuffing toe op 0x1B-bytes."""
    stuffed = bytearray()
    for b in message:
        stuffed.append(b)
        if b == _ESC:
            stuffed.append(_ESC)
    return bytes([_ESC, _SOH]) + bytes(stuffed) + bytes([_ESC, _NL])


def _encode_can_id(priority: int, pgn: int, source: int, destination: int = 0xFF) -> int:
    """Inverse van ebl_reader._parse_can_id — geverifieerd tegen het bekende voorbeeld hieronder."""
    dp = (pgn >> 16) & 0x1
    pf = (pgn >> 8) & 0xFF
    ps = destination if pf < 240 else pgn & 0xFF
    return source | (ps << 8) | (pf << 16) | (dp << 24) | (priority << 26)


def test_encode_can_id_matches_verified_example():
    # 1b 01 07 95 0e 28 9a 00 01 f8 09 ... -> canid-bytes 00 01 f8 09 -> src0/dst255/pgn129025/prio2
    assert _encode_can_id(priority=2, pgn=129025, source=0, destination=255) == 0x09F80100


def _bst95_record(can_id: int, payload: bytes) -> bytes:
    """Bouwt een compleet '07 95 <len> <tijdteller> <canid> <payload>'-record (vóór omkadering)."""
    body = struct.pack("<H", 0) + struct.pack("<I", can_id) + payload  # tijdteller wordt genegeerd
    length = len(body)
    return bytes([0x07, 0x95, length]) + body


def _system_time_record(when: datetime, priority: int = 3, source: int = 0) -> bytes:
    can_id = _encode_can_id(priority, 126992, source)
    epoch_days = (when.date() - date(1970, 1, 1)).days
    seconds_of_day = (when - datetime.combine(when.date(), datetime.min.time())).total_seconds()
    time_raw = round(seconds_of_day / 0.0001)
    payload = struct.pack("<BBH", 0, 0, epoch_days) + struct.pack("<I", time_raw)
    return _bst95_record(can_id, payload)


def _fast_packet_records(can_id: int, full_payload: bytes, seq_counter: int = 0):
    records = []
    total_length = len(full_payload)
    first = bytes([(seq_counter << 5) | 0, total_length]) + full_payload[:6]
    records.append(_bst95_record(can_id, first))
    remaining = full_payload[6:]
    frame_index = 1
    while remaining:
        chunk, remaining = remaining[:7], remaining[7:]
        header = bytes([(seq_counter << 5) | frame_index])
        records.append(_bst95_record(can_id, header + chunk))
        frame_index += 1
    return records


def test_iter_frames_verified_reference_example(tmp_path: Path):
    """Exact het voorbeeld uit go-nmea-client's eblreader_test.go (met bevestigde uitkomst)."""
    system_time = _system_time_record(datetime(2026, 7, 15, 9, 0, 0))
    position_record = bytes.fromhex("07950e289a0001f8093d0db3224832590d")
    data = _frame_bytes(system_time) + _frame_bytes(position_record)
    path = tmp_path / "test.ebl"
    path.write_bytes(data)

    frames = list(iter_frames(path))

    assert len(frames) == 1
    frame = frames[0]
    assert frame.pgn == 129025
    assert frame.priority == 2
    assert frame.source == 0
    assert frame.destination == 255
    assert frame.data == bytes.fromhex("3d0db3224832590d")


def test_iter_frames_before_system_time_are_dropped(tmp_path: Path):
    position_record = bytes.fromhex("07950e289a0001f8093d0db3224832590d")
    data = _frame_bytes(position_record)  # geen System Time hiervoor
    path = tmp_path / "test.ebl"
    path.write_bytes(data)

    assert list(iter_frames(path)) == []


def test_iter_frames_byte_stuffing(tmp_path: Path):
    # data bevat expres een 0x1B-byte, die in het bestand dubbel moet voorkomen
    payload_with_esc = bytes([0x00, 0x1B, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07])
    record = _bst95_record(_encode_can_id(priority=2, pgn=129025, source=0), payload_with_esc)

    when = datetime(2026, 7, 15, 10, 0, 0)
    data = _frame_bytes(_system_time_record(when)) + _frame_bytes(record)
    path = tmp_path / "test.ebl"
    path.write_bytes(data)

    frames = list(iter_frames(path))

    assert len(frames) == 1
    assert frames[0].data == payload_with_esc


def test_iter_frames_fast_packet_reassembly(tmp_path: Path):
    # PGN 127489 (Engine Parameters, Dynamic), 26 bytes -> verspreid over 4 CAN-frames
    full_payload = struct.pack(
        "<BHHHhhIHHBHHbb",
        0, 0xFFFF, 0xFFFF, 0xFFFF, 0x7FFF, 68, 36000, 0xFFFF, 0xFFFF, 0xFF, 0xFFFF, 0xFFFF, 0x7F, 0x7F,
    )
    assert len(full_payload) == 26

    when = datetime(2026, 7, 15, 11, 0, 0)
    data = bytearray()
    data += _frame_bytes(_system_time_record(when))
    engine_can_id = _encode_can_id(priority=2, pgn=127489, source=1)
    for record in _fast_packet_records(engine_can_id, full_payload):
        data += _frame_bytes(record)

    path = tmp_path / "test.ebl"
    path.write_bytes(bytes(data))

    frames = list(iter_frames(path))

    assert len(frames) == 1
    frame = frames[0]
    assert frame.pgn == 127489
    assert frame.data == full_payload


def test_iter_frames_time_updates_between_system_time_messages(tmp_path: Path):
    t1 = datetime(2026, 7, 15, 9, 0, 0)
    t2 = datetime(2026, 7, 15, 9, 5, 0)
    position_payload = struct.pack("<ii", 1000000, 2000000)
    position_record = _bst95_record(_encode_can_id(priority=2, pgn=129025, source=0), position_payload)

    data = bytearray()
    data += _frame_bytes(_system_time_record(t1))
    data += _frame_bytes(position_record)
    data += _frame_bytes(_system_time_record(t2))
    data += _frame_bytes(position_record)

    path = tmp_path / "test.ebl"
    path.write_bytes(bytes(data))

    frames = list(iter_frames(path))

    assert len(frames) == 2
    assert frames[0].time == t1
    assert frames[1].time == t2
