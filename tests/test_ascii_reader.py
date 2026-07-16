from datetime import date, datetime
from pathlib import Path

from nmea2000processor.ascii_reader import iter_frames


def test_parse_basic_line(tmp_path: Path):
    line = "A173321.107 23FF7 1F513 012F3070002F30709F\n"
    log_path = tmp_path / "test.raw"
    log_path.write_text(line, encoding="ascii")

    frames = list(iter_frames(log_path, start_date=date(2026, 7, 15)))

    assert len(frames) == 1
    frame = frames[0]
    assert frame.time == datetime(2026, 7, 15, 17, 33, 21, 107000)
    assert frame.source == 0x23
    assert frame.destination == 0xFF
    assert frame.priority == 0x7
    assert frame.pgn == (0x1F513 & 0x3FFFF)
    assert frame.data == bytes.fromhex("012F3070002F30709F")


def test_ignores_malformed_and_non_a_lines(tmp_path: Path):
    lines = [
        "$GPGGA,some,nmea0183,line\n",
        "not a valid line at all\n",
        "A173321.107 23FF7 1F513 00\n",
    ]
    log_path = tmp_path / "test.raw"
    log_path.write_text("".join(lines), encoding="ascii")

    frames = list(iter_frames(log_path, start_date=date(2026, 7, 15)))

    assert len(frames) == 1


def test_midnight_rollover(tmp_path: Path):
    lines = [
        "A235959.000 23FF7 1F513 00\n",
        "A000001.000 23FF7 1F513 00\n",
    ]
    log_path = tmp_path / "test.raw"
    log_path.write_text("".join(lines), encoding="ascii")

    frames = list(iter_frames(log_path, start_date=date(2026, 7, 15)))

    assert frames[0].time.date() == date(2026, 7, 15)
    assert frames[1].time.date() == date(2026, 7, 16)


def test_guesses_start_date_from_filename(tmp_path: Path):
    log_path = tmp_path / "2026-03-02.raw"
    log_path.write_text("A000000.000 23FF7 1F513 00\n", encoding="ascii")

    frames = list(iter_frames(log_path))

    assert frames[0].time.date() == date(2026, 3, 2)
