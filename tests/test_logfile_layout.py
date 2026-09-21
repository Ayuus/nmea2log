from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pytest

from nmea2log import logfile_layout, pipeline
from nmea2log.logfile_layout import check_logfile_layout, log_logfile_layout
from nmea2log.model import PositionFix
from nmea2log.sample_cache import SampleCache


def _archive(root: Path, folders: Dict[int, List[int]]) -> List[Path]:
    """Creates ``EBL{n}/{n}_{seq}.ebl`` files; returns their paths in archive order."""
    paths = []
    for folder, sequences in sorted(folders.items()):
        directory = root / f"EBL{folder:06d}"
        directory.mkdir(parents=True, exist_ok=True)
        for sequence in sequences:
            path = directory / f"{folder:06d}_{sequence:03d}.ebl"
            path.write_bytes(b"x" * 10)
            paths.append(path)
    return paths


def _full(count: int = 100) -> List[int]:
    return list(range(count))


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(logfile_layout, "log", lambda message, **kwargs: lines.append(message))
    monkeypatch.setattr(pipeline, "log", lambda message, **kwargs: lines.append(message))
    return lines


def test_a_complete_archive_has_no_problems(tmp_path: Path):
    files = _archive(tmp_path, {0: _full(), 1: _full(), 2: _full(26)})

    assert check_logfile_layout(files) == []


def test_a_complete_archive_is_reported_with_its_totals(tmp_path: Path, logged):
    files = _archive(tmp_path, {0: _full(), 1: _full(26)})

    log_logfile_layout(files)

    assert logged == ["[info] Log files: 126 .ebl file(s) in 2 folder(s) (EBL000000 to EBL000001), none missing."]


def test_a_gap_inside_a_folder_names_the_missing_file(tmp_path: Path):
    files = _archive(tmp_path, {0: [s for s in _full() if s != 42], 1: _full(10)})

    assert check_logfile_layout(files) == ["EBL000000: 1 file(s) missing: 000000_042.ebl"]


def test_a_full_folder_that_ends_early_is_missing_its_last_files(tmp_path: Path):
    files = _archive(tmp_path, {0: _full(98), 1: _full(5)})

    assert check_logfile_layout(files) == ["EBL000000: 2 file(s) missing: 000000_098.ebl, 000000_099.ebl"]


def test_the_newest_folder_may_be_short(tmp_path: Path):
    assert check_logfile_layout(_archive(tmp_path, {0: _full(), 1: _full(1)})) == []


def test_the_oldest_folder_may_start_later(tmp_path: Path):
    assert check_logfile_layout(_archive(tmp_path, {5: list(range(30, 100)), 6: _full(3)})) == []


def test_a_missing_folder_is_reported(tmp_path: Path):
    files = _archive(tmp_path, {0: _full(), 2: _full(4)})

    assert check_logfile_layout(files) == ["folder(s) missing: EBL000001"]


def test_a_misnamed_file_and_a_file_in_the_wrong_folder_are_reported(tmp_path: Path):
    files = _archive(tmp_path, {0: _full(), 1: _full(3)})
    bad_name = tmp_path / "EBL000001" / "copy of 000001_002.ebl"
    bad_name.write_bytes(b"x")
    wrong_folder = tmp_path / "EBL000000" / "000007_003.ebl"
    wrong_folder.write_bytes(b"x")

    problems = check_logfile_layout(files + [bad_name, wrong_folder])

    assert "EBL000001/copy of 000001_002.ebl: the name is not of the form NNNNNN_MMM.ebl" in problems
    assert "EBL000000/000007_003.ebl: the number in the name does not match its folder" in problems


def test_many_missing_files_are_listed_briefly(tmp_path: Path):
    files = _archive(tmp_path, {0: _full(20), 1: _full(2)})

    (problem,) = check_logfile_layout(files)

    assert problem.startswith("EBL000000: 80 file(s) missing: 000000_020.ebl, 000000_021.ebl")
    assert problem.endswith("and 74 more")


def test_problems_are_logged_as_anomalies(tmp_path: Path, logged):
    log_logfile_layout(_archive(tmp_path, {0: [s for s in _full() if s != 3], 1: _full(2)}))

    assert len(logged) == 1
    assert logged[0].startswith("[anomaly] Log files: EBL000000: 1 file(s) missing: 000000_003.ebl")


def test_files_outside_an_ebl_folder_are_not_checked(tmp_path: Path, logged):
    flat = [tmp_path / "000000_000.ebl", tmp_path / "000000_005.ebl"]
    for path in flat:
        path.write_bytes(b"x")

    assert check_logfile_layout(flat) == []
    log_logfile_layout(flat)
    assert logged == []


# --- the decoded files (sample cache entries), folder by folder ------------------------------------------


def _cache_with(tmp_path: Path, files: List[Path], skip=()) -> SampleCache:
    cache = SampleCache(tmp_path / "cache")
    samples = ({10: [PositionFix(datetime(2026, 7, 15, 9, 0), 52.3, 4.9)]}, {}, [], [], {}, {}, {}, [], {})
    for path in files:
        if path.name not in skip:
            cache.put(path, samples, None)
    return cache


def test_every_decoded_file_in_the_cache_is_reported_with_its_totals(tmp_path: Path, logged):
    files = _archive(tmp_path, {0: _full(3), 1: _full(2)})

    pipeline._log_decoded_file_counts(files, 0, _cache_with(tmp_path, files))

    assert logged == ["[info] Decoded samples: 5/5 file(s) in 2 folder(s) are in the sample cache."]


def test_a_decoded_file_without_a_cache_entry_is_reported_per_folder(tmp_path: Path, logged):
    files = _archive(tmp_path, {0: _full(3), 1: _full(2)})

    pipeline._log_decoded_file_counts(files, 0, _cache_with(tmp_path, files, skip={"000001_001.ebl"}))

    assert logged == ["[anomaly] Decoded samples: EBL000001: 1/2 file(s) are in the sample cache, missing: 000001_001.ebl"]


def test_files_before_the_resume_point_are_not_expected_to_have_been_decoded(tmp_path: Path, logged):
    files = _archive(tmp_path, {0: _full(3), 1: _full(2)})
    cache = _cache_with(tmp_path, files, skip={"000000_000.ebl", "000000_001.ebl"})

    pipeline._log_decoded_file_counts(files, 2, cache)

    assert logged == ["[info] Decoded samples: 3/3 file(s) in 2 folder(s) are in the sample cache."]


def test_cache_entries_of_files_no_longer_in_the_archive_are_noted(tmp_path: Path, logged):
    files = _archive(tmp_path, {0: _full(3)})
    cache = _cache_with(tmp_path, files)
    extra = tmp_path / "EBL000009" / "000009_000.ebl"
    extra.parent.mkdir()
    extra.write_bytes(b"x")
    _cache_with(tmp_path, [extra])  # same cache directory: an entry for a file that is then removed
    extra.unlink()

    pipeline._log_decoded_file_counts(files, 0, cache)

    assert logged[-1] == "[info] Sample cache: 1 entrie(s) belong to .ebl files that are no longer in the archive."
