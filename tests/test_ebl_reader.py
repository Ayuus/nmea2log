import struct
from datetime import date, datetime, timedelta
from pathlib import Path

from nmea2log.ebl_reader import iter_frames

_ESC = 0x1B
_SOH = 0x01
_NL = 0x0A


def _frame_bytes(message: bytes) -> bytes:
    """Wraps a record with ESC/SOH...ESC/NL and applies byte stuffing to 0x1B bytes."""
    stuffed = bytearray()
    for b in message:
        stuffed.append(b)
        if b == _ESC:
            stuffed.append(_ESC)
    return bytes([_ESC, _SOH]) + bytes(stuffed) + bytes([_ESC, _NL])


def _encode_can_id(priority: int, pgn: int, source: int, destination: int = 0xFF) -> int:
    """Inverse of ebl_reader._parse_can_id -- verified against the known example below."""
    dp = (pgn >> 16) & 0x1
    pf = (pgn >> 8) & 0xFF
    ps = destination if pf < 240 else pgn & 0xFF
    return source | (ps << 8) | (pf << 16) | (dp << 24) | (priority << 26)


def test_encode_can_id_matches_verified_example():
    # 1b 01 07 95 0e 28 9a 00 01 f8 09 ... -> canid-bytes 00 01 f8 09 -> src0/dst255/pgn129025/prio2
    assert _encode_can_id(priority=2, pgn=129025, source=0, destination=255) == 0x09F80100


def _bst95_record(can_id: int, payload: bytes) -> bytes:
    """Builds a complete '07 95 <len> <time counter> <canid> <payload>' record (before wrapping)."""
    body = struct.pack("<H", 0) + struct.pack("<I", can_id) + payload  # time counter is ignored
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
    """Exactly the example from go-nmea-client's eblreader_test.go (with confirmed outcome)."""
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
    data = _frame_bytes(position_record)  # no System Time before this
    path = tmp_path / "test.ebl"
    path.write_bytes(data)

    assert list(iter_frames(path)) == []


def test_iter_frames_wanted_pgns_filters_out_unrequested_pgns(tmp_path: Path):
    when = datetime(2026, 7, 15, 9, 0, 0)
    position_record = bytes.fromhex("07950e289a0001f8093d0db3224832590d")  # pgn 129025
    depth_record = _bst95_record(
        _encode_can_id(priority=3, pgn=128267, source=1), bytes([0, 0, 0, 100, 0, 0, 0, 0])
    )
    data = _frame_bytes(_system_time_record(when)) + _frame_bytes(position_record) + _frame_bytes(depth_record)
    path = tmp_path / "test.ebl"
    path.write_bytes(data)

    frames = list(iter_frames(path, wanted_pgns=frozenset({128267})))

    assert [f.pgn for f in frames] == [128267]  # 129025 dropped, not requested


def test_iter_frames_wanted_pgns_always_keeps_system_time_working(tmp_path: Path):
    """PGN 126992 must keep updating the internal time reference even when it's not itself in
    wanted_pgns -- otherwise every later frame would silently have no timestamp to attach to."""
    when = datetime(2026, 7, 15, 9, 0, 0)
    position_record = bytes.fromhex("07950e289a0001f8093d0db3224832590d")
    data = _frame_bytes(_system_time_record(when)) + _frame_bytes(position_record)
    path = tmp_path / "test.ebl"
    path.write_bytes(data)

    frames = list(iter_frames(path, wanted_pgns=frozenset({129025})))

    assert len(frames) == 1
    assert frames[0].time == when


def test_iter_frames_byte_stuffing(tmp_path: Path):
    # the data deliberately contains a 0x1B byte, which must appear doubled in the file
    payload_with_esc = bytes([0x00, 0x1B, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07])
    record = _bst95_record(_encode_can_id(priority=2, pgn=129025, source=0), payload_with_esc)

    when = datetime(2026, 7, 15, 10, 0, 0)
    data = _frame_bytes(_system_time_record(when)) + _frame_bytes(record)
    path = tmp_path / "test.ebl"
    path.write_bytes(data)

    frames = list(iter_frames(path))

    assert len(frames) == 1
    assert frames[0].data == payload_with_esc


def test_iter_frames_recovers_after_unknown_escape_sequence(tmp_path: Path):
    """An ESC followed by anything other than another ESC (byte stuffing) or NL (end of record)
    is a malformed escape; that one record is discarded, but parsing must recover and pick up
    the next valid record normally."""
    when = datetime(2026, 7, 15, 10, 0, 0)
    position_record = bytes.fromhex("07950e289a0001f8093d0db3224832590d")
    good = _frame_bytes(position_record)
    # a "record" containing ESC followed by a byte that's neither ESC nor NL
    garbage = bytes([_ESC, _SOH]) + bytes([0x01, 0x02, _ESC, 0x99]) + bytes([_ESC, _NL])

    data = _frame_bytes(_system_time_record(when)) + garbage + good
    path = tmp_path / "test.ebl"
    path.write_bytes(data)

    frames = list(iter_frames(path))

    assert len(frames) == 1
    assert frames[0].pgn == 129025


def test_iter_frames_drops_truncated_record_at_eof(tmp_path: Path):
    """A record that never reaches its closing ESC+NL (e.g. the file was cut off mid-write)
    yields nothing for that fragment, instead of crashing or hanging."""
    when = datetime(2026, 7, 15, 10, 0, 0)
    position_record = bytes.fromhex("07950e289a0001f8093d0db3224832590d")

    data = _frame_bytes(_system_time_record(when)) + bytes([_ESC, _SOH]) + position_record
    path = tmp_path / "test.ebl"
    path.write_bytes(data)

    assert list(iter_frames(path)) == []


def test_iter_frames_fast_packet_reassembly(tmp_path: Path):
    # PGN 127489 (Engine Parameters, Dynamic), 26 bytes -> spread over 4 CAN frames
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


def test_iter_frames_carries_time_across_files_via_time_state(tmp_path: Path):
    """Regression test: a file without its own System Time message (e.g. anchored for a long
    time with GPS/plotter idle, but autopilot/gyro still coming in) must not be silently
    discarded entirely when the time is already known from an earlier file in the same
    session."""
    when = datetime(2026, 7, 29, 12, 0, 0)
    position_payload = struct.pack("<ii", 1000000, 2000000)
    position_record = _bst95_record(_encode_can_id(priority=2, pgn=129025, source=0), position_payload)

    file1 = tmp_path / "file1.ebl"
    file1.write_bytes(bytes(_frame_bytes(_system_time_record(when)) + _frame_bytes(position_record)))

    # file2 contains NO System Time message, only another position message (like during an
    # anchoring period with quiet GPS but an active autopilot/gyro).
    file2 = tmp_path / "file2.ebl"
    file2.write_bytes(bytes(_frame_bytes(position_record)))

    time_state: dict = {}
    frames_file1 = list(iter_frames(file1, time_state=time_state))
    frames_file2 = list(iter_frames(file2, time_state=time_state))

    assert len(frames_file1) == 1
    assert len(frames_file2) == 1  # without the fix: 0, since file2 has no time reference of its own
    assert frames_file2[0].time == when


def test_iter_frames_without_time_state_starts_fresh_per_file(tmp_path: Path):
    """Default behavior (no time_state supplied) stays unchanged: each file on its own."""
    position_payload = struct.pack("<ii", 1000000, 2000000)
    position_record = _bst95_record(_encode_can_id(priority=2, pgn=129025, source=0), position_payload)

    file1 = tmp_path / "file1.ebl"
    file1.write_bytes(
        bytes(_frame_bytes(_system_time_record(datetime(2026, 7, 29, 12, 0, 0))) + _frame_bytes(position_record))
    )
    file2 = tmp_path / "file2.ebl"
    file2.write_bytes(bytes(_frame_bytes(position_record)))

    assert len(list(iter_frames(file1))) == 1
    assert list(iter_frames(file2)) == []
