import struct
from datetime import date, datetime
from pathlib import Path

from nmea2log.import_ebl import first_ebl_timestamp, import_staged_ebl_files

_ESC = 0x1B
_SOH = 0x01
_NL = 0x0A


def _frame_bytes(message: bytes) -> bytes:
    stuffed = bytearray()
    for b in message:
        stuffed.append(b)
        if b == _ESC:
            stuffed.append(_ESC)
    return bytes([_ESC, _SOH]) + bytes(stuffed) + bytes([_ESC, _NL])


def _encode_can_id(priority: int, pgn: int, source: int, destination: int = 0xFF) -> int:
    dp = (pgn >> 16) & 0x1
    pf = (pgn >> 8) & 0xFF
    ps = destination if pf < 240 else pgn & 0xFF
    return source | (ps << 8) | (pf << 16) | (dp << 24) | (priority << 26)


def _bst95_record(can_id: int, payload: bytes) -> bytes:
    body = struct.pack("<H", 0) + struct.pack("<I", can_id) + payload
    length = len(body)
    return bytes([0x07, 0x95, length]) + body


def _system_time_record(when: datetime, priority: int = 3, source: int = 0) -> bytes:
    can_id = _encode_can_id(priority, 126992, source)
    epoch_days = (when.date() - date(1970, 1, 1)).days
    seconds_of_day = (when - datetime.combine(when.date(), datetime.min.time())).total_seconds()
    time_raw = round(seconds_of_day / 0.0001)
    payload = struct.pack("<BBH", 0, 0, epoch_days) + struct.pack("<I", time_raw)
    return _bst95_record(can_id, payload)


# A real PGN 129025 (Position, Rapid Update) record -- the same verified example
# test_ebl_reader.py's own test_iter_frames_verified_reference_example uses -- so a file with a
# System Time message followed by this one has something first_ebl_timestamp() (via iter_frames'
# own _WANTED_PGNS filter) actually yields a Frame for.
_POSITION_RECORD = bytes.fromhex("07950e289a0001f8093d0db3224832590d")


def _ebl_bytes(when: datetime) -> bytes:
    return _frame_bytes(_system_time_record(when)) + _frame_bytes(_POSITION_RECORD)


def _write_ebl(path: Path, when: datetime) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_ebl_bytes(when))
    return path


def test_first_ebl_timestamp_reads_the_files_own_system_time(tmp_path: Path):
    path = _write_ebl(tmp_path / "test.ebl", datetime(2026, 7, 15, 9, 0, 0))
    assert first_ebl_timestamp(path) == datetime(2026, 7, 15, 9, 0, 0)


def test_first_ebl_timestamp_is_none_without_a_system_time_message(tmp_path: Path):
    path = tmp_path / "test.ebl"
    path.write_bytes(_frame_bytes(_POSITION_RECORD))  # no System Time record at all
    assert first_ebl_timestamp(path) is None


def test_a_normal_ebl_folder_lands_under_its_own_year(tmp_path: Path):
    staging = tmp_path / "staging"
    dest = tmp_path / "Actisense"
    _write_ebl(staging / "EBL000000" / "000000_000.ebl", datetime(2026, 3, 1, 8, 0, 0))
    _write_ebl(staging / "EBL000000" / "000000_001.ebl", datetime(2026, 3, 1, 9, 0, 0))

    result = import_staged_ebl_files(
        [str(staging / "EBL000000" / "000000_000.ebl"), str(staging / "EBL000000" / "000000_001.ebl")],
        str(dest),
    )

    assert result == {
        "imported": 2,
        "skipped_duplicate": 0,
        "renamed": [],
        "errors": [],
        "files": [
            {"name": "000000_000.ebl", "outcome": "imported"},
            {"name": "000000_001.ebl", "outcome": "imported"},
        ],
    }
    assert (dest / "2026" / "EBL000000" / "000000_000.ebl").read_bytes() == _ebl_bytes(datetime(2026, 3, 1, 8, 0, 0))
    assert (dest / "2026" / "EBL000000" / "000000_001.ebl").exists()


def test_progress_callback_fires_once_per_file_as_each_one_actually_lands(tmp_path: Path):
    """The whole reason this callback exists: a platform-side log/progress bar has to be able to
    show files landing one at a time as the real copying happens, not replay an already-finished
    list after import_staged_ebl_files() has already returned (see ImportProgressCallback's own
    doc comment) -- so this asserts the callback fires *during* the call, with the right
    1-based current/total and each file's own real outcome, not just that it fires at all."""
    staging = tmp_path / "staging"
    dest = tmp_path / "Actisense"
    _write_ebl(staging / "EBL000000" / "000000_000.ebl", datetime(2026, 3, 1, 8, 0, 0))
    _write_ebl(staging / "EBL000000" / "000000_001.ebl", datetime(2026, 3, 1, 9, 0, 0))
    paths = [str(staging / "EBL000000" / "000000_000.ebl"), str(staging / "EBL000000" / "000000_001.ebl")]

    # First import: both files land as fresh "imported".
    calls = []
    import_staged_ebl_files(paths, str(dest), progress_callback=lambda *args: calls.append(args))
    assert calls == [
        (1, 2, "000000_000.ebl", "imported"),
        (2, 2, "000000_001.ebl", "imported"),
    ]

    # Re-importing the same files: both now report as duplicates, proving the callback reflects
    # each file's own real, just-decided outcome rather than a fixed/guessed value.
    calls = []
    import_staged_ebl_files(paths, str(dest), progress_callback=lambda *args: calls.append(args))
    assert calls == [
        (1, 2, "000000_000.ebl", "skipped_duplicate"),
        (2, 2, "000000_001.ebl", "skipped_duplicate"),
    ]


def test_a_folder_spanning_new_years_eve_stays_together_under_its_start_year(tmp_path: Path):
    staging = tmp_path / "staging"
    dest = tmp_path / "Actisense"
    _write_ebl(staging / "EBL000000" / "000000_000.ebl", datetime(2025, 12, 31, 23, 0, 0))
    _write_ebl(staging / "EBL000000" / "000000_001.ebl", datetime(2026, 1, 1, 0, 30, 0))

    result = import_staged_ebl_files(
        [str(staging / "EBL000000" / "000000_000.ebl"), str(staging / "EBL000000" / "000000_001.ebl")],
        str(dest),
    )

    assert result["imported"] == 2
    assert (dest / "2025" / "EBL000000" / "000000_000.ebl").exists()
    assert (dest / "2025" / "EBL000000" / "000000_001.ebl").exists()  # same folder as its sibling, not 2026


def test_reimporting_the_same_card_is_a_noop(tmp_path: Path):
    staging = tmp_path / "staging"
    dest = tmp_path / "Actisense"
    _write_ebl(staging / "EBL000000" / "000000_000.ebl", datetime(2026, 3, 1, 8, 0, 0))
    paths = [str(staging / "EBL000000" / "000000_000.ebl")]

    first = import_staged_ebl_files(paths, str(dest))
    second = import_staged_ebl_files(paths, str(dest))

    assert first == {
        "imported": 1,
        "skipped_duplicate": 0,
        "renamed": [],
        "errors": [],
        "files": [{"name": "000000_000.ebl", "outcome": "imported"}],
    }
    assert second == {
        "imported": 0,
        "skipped_duplicate": 1,
        "renamed": [],
        "errors": [],
        "files": [{"name": "000000_000.ebl", "outcome": "skipped_duplicate"}],
    }


def test_a_reformatted_sd_card_reusing_a_folder_name_gets_a_renamed_folder_not_a_silent_alias(tmp_path: Path):
    staging = tmp_path / "staging"
    dest = tmp_path / "Actisense"
    # First card: EBL000000 with one file, imported normally.
    _write_ebl(staging / "EBL000000" / "000000_000.ebl", datetime(2025, 6, 1, 8, 0, 0))
    import_staged_ebl_files([str(staging / "EBL000000" / "000000_000.ebl")], str(dest))

    # The SD card gets reformatted; the W2K-2 starts over at EBL000000 with unrelated content
    # (different bytes, a later date -- a real reformat wouldn't produce identical files).
    staging2 = tmp_path / "staging2"
    _write_ebl(staging2 / "EBL000000" / "000000_000.ebl", datetime(2026, 5, 1, 8, 0, 0))
    # Make sure the two "000000_000.ebl" files really do differ in content, not just timestamp
    # metadata a size-only check might miss.
    assert (staging / "EBL000000" / "000000_000.ebl").read_bytes() != (
        staging2 / "EBL000000" / "000000_000.ebl"
    ).read_bytes()

    result = import_staged_ebl_files([str(staging2 / "EBL000000" / "000000_000.ebl")], str(dest))

    assert result["imported"] == 1
    assert result["skipped_duplicate"] == 0
    # The original import is untouched, and still exactly what it was.
    original = dest / "2025" / "EBL000000" / "000000_000.ebl"
    assert original.exists()
    assert original.read_bytes() == _ebl_bytes(datetime(2025, 6, 1, 8, 0, 0))
    # The reformatted card's own EBL000000 landed under its own year, renamed so it can never be
    # confused for the original by sample_cache.py's/logfile_layout.py's own EBLnnnnnn matching.
    renamed_dirs = list((dest / "2026").glob("EBL000000-*"))
    assert len(renamed_dirs) == 1
    assert (renamed_dirs[0] / "000000_000.ebl").read_bytes() == _ebl_bytes(datetime(2026, 5, 1, 8, 0, 0))
    assert not (dest / "2026" / "EBL000000").exists()
    # Reported back so the platform layer can log this more alarmingly than a plain import --
    # two same-named files turned out to hold different data.
    assert result["renamed"] == [f"EBL000000 -> {renamed_dirs[0].name}"]


def test_a_growing_folder_can_be_imported_again_with_more_files_without_renaming(tmp_path: Path):
    staging = tmp_path / "staging"
    dest = tmp_path / "Actisense"
    _write_ebl(staging / "EBL000000" / "000000_000.ebl", datetime(2026, 3, 1, 8, 0, 0))
    import_staged_ebl_files([str(staging / "EBL000000" / "000000_000.ebl")], str(dest))

    # The same card, still not reformatted, now has one more file in the same still-open folder.
    _write_ebl(staging / "EBL000000" / "000000_001.ebl", datetime(2026, 3, 1, 9, 0, 0))
    result = import_staged_ebl_files(
        [str(staging / "EBL000000" / "000000_000.ebl"), str(staging / "EBL000000" / "000000_001.ebl")],
        str(dest),
    )

    assert result == {
        "imported": 1,
        "skipped_duplicate": 1,
        "renamed": [],
        "errors": [],
        "files": [
            {"name": "000000_000.ebl", "outcome": "skipped_duplicate"},
            {"name": "000000_001.ebl", "outcome": "imported"},
        ],
    }
    assert (dest / "2026" / "EBL000000" / "000000_001.ebl").exists()
    assert not list(dest.rglob("EBL000000-*"))  # no rename needed: nothing actually conflicted


def test_a_folder_already_downloaded_normally_without_a_year_layer_is_recognized_not_duplicated(
    tmp_path: Path,
):
    """A real bug, found in practice: every normal W2K-2 download (before this import feature
    existed, and still today) writes straight to ``Actisense/EBLnnnnnn``, no year folder above it.
    Importing more of the same folder must land there too, not silently fail to recognize it and
    duplicate the whole folder under a fresh year layer instead."""
    dest = tmp_path / "Actisense"
    # A normal download already sitting there, exactly as EblStorage.downloadDir()/self.ebl_dir()
    # itself would have put it -- no year layer.
    _write_ebl(dest / "EBL000000" / "000000_000.ebl", datetime(2026, 3, 1, 8, 0, 0))

    staging = tmp_path / "staging"
    # The same file again (e.g. a backup SD card holding the same season) plus one genuinely new
    # one from the same still-open folder.
    _write_ebl(staging / "EBL000000" / "000000_000.ebl", datetime(2026, 3, 1, 8, 0, 0))
    _write_ebl(staging / "EBL000000" / "000000_001.ebl", datetime(2026, 3, 1, 9, 0, 0))

    result = import_staged_ebl_files(
        [str(staging / "EBL000000" / "000000_000.ebl"), str(staging / "EBL000000" / "000000_001.ebl")],
        str(dest),
    )

    assert result["imported"] == 1
    assert result["skipped_duplicate"] == 1
    assert result["renamed"] == []
    # Landed in the original, un-prefixed folder -- not duplicated under a new "2026/EBL000000".
    assert (dest / "EBL000000" / "000000_001.ebl").exists()
    assert not (dest / "2026").exists()


def test_a_folder_already_downloaded_normally_that_actually_conflicts_gets_renamed_not_merged(
    tmp_path: Path,
):
    """Same starting point as the test above, but this time the reformatted-SD-card scenario:
    the import's own EBL000000 holds different content than what was already downloaded
    normally. The existing, un-prefixed folder must stay untouched; the conflicting import gets
    its own, separately named home instead of silently overwriting real data."""
    dest = tmp_path / "Actisense"
    _write_ebl(dest / "EBL000000" / "000000_000.ebl", datetime(2025, 6, 1, 8, 0, 0))

    staging = tmp_path / "staging"
    _write_ebl(staging / "EBL000000" / "000000_000.ebl", datetime(2026, 5, 1, 8, 0, 0))

    result = import_staged_ebl_files([str(staging / "EBL000000" / "000000_000.ebl")], str(dest))

    assert result["imported"] == 1
    original = dest / "EBL000000" / "000000_000.ebl"
    assert original.read_bytes() == _ebl_bytes(datetime(2025, 6, 1, 8, 0, 0))
    renamed_dirs = list((dest / "2026").glob("EBL000000-*"))
    assert len(renamed_dirs) == 1
    assert (renamed_dirs[0] / "000000_000.ebl").read_bytes() == _ebl_bytes(datetime(2026, 5, 1, 8, 0, 0))


def test_a_file_with_no_ebl_folder_structure_lands_loose_under_its_own_year(tmp_path: Path):
    staging = tmp_path / "staging"
    dest = tmp_path / "Actisense"
    _write_ebl(staging / "some_random_name.ebl", datetime(2026, 4, 2, 10, 0, 0))

    result = import_staged_ebl_files([str(staging / "some_random_name.ebl")], str(dest))

    assert result == {
        "imported": 1,
        "skipped_duplicate": 0,
        "renamed": [],
        "errors": [],
        "files": [{"name": "some_random_name.ebl", "outcome": "imported"}],
    }
    assert (dest / "2026" / "some_random_name.ebl").exists()
    assert not (dest / "2026" / "EBL000000").exists()


def test_two_loose_files_with_the_same_name_but_different_content_are_both_kept_and_reported(tmp_path: Path):
    staging = tmp_path / "staging"
    dest = tmp_path / "Actisense"
    _write_ebl(staging / "some_random_name.ebl", datetime(2026, 4, 2, 10, 0, 0))
    import_staged_ebl_files([str(staging / "some_random_name.ebl")], str(dest))

    # A second, unrelated SD card that happens to use the exact same filename for something else.
    staging2 = tmp_path / "staging2"
    _write_ebl(staging2 / "some_random_name.ebl", datetime(2026, 4, 2, 11, 0, 0))
    result = import_staged_ebl_files([str(staging2 / "some_random_name.ebl")], str(dest))

    assert result["imported"] == 1
    assert result["skipped_duplicate"] == 0
    assert result["renamed"] == ["some_random_name.ebl -> some_random_name-1.ebl"]
    assert (dest / "2026" / "some_random_name.ebl").read_bytes() == _ebl_bytes(datetime(2026, 4, 2, 10, 0, 0))
    assert (dest / "2026" / "some_random_name-1.ebl").read_bytes() == _ebl_bytes(datetime(2026, 4, 2, 11, 0, 0))


def test_a_file_with_no_system_time_message_lands_under_an_unknown_year_bucket(tmp_path: Path):
    staging = tmp_path / "staging"
    dest = tmp_path / "Actisense"
    path = staging / "no_time.ebl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_frame_bytes(_POSITION_RECORD))  # no System Time record

    result = import_staged_ebl_files([str(path)], str(dest))

    assert result == {
        "imported": 1,
        "skipped_duplicate": 0,
        "renamed": [],
        "errors": [],
        "files": [{"name": "no_time.ebl", "outcome": "imported"}],
    }
    assert (dest / "onbekend" / "no_time.ebl").exists()
