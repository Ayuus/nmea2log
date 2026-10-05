from pathlib import Path

from nmea2log import ebl_storage


def _make(root: Path, relative: str, size: int) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_ebl_files_finds_them_at_any_depth_in_any_case_and_nothing_else(tmp_path: Path):
    _make(tmp_path, "2025/EBL000001/000001_001.ebl", 10)
    _make(tmp_path, "EBL000002/000002_001.EBL", 10)
    _make(tmp_path, "2025/notes.txt", 10)

    names = [p.name for p in ebl_storage.ebl_files(tmp_path)]

    assert names == ["000001_001.ebl", "000002_001.EBL"]


def test_a_missing_folder_has_no_files(tmp_path: Path):
    assert ebl_storage.ebl_files(tmp_path / "nope") == []
    assert ebl_storage.describe(tmp_path / "nope") == (0, "0 B")


def test_format_size_uses_decimal_units():
    assert ebl_storage.format_size(0) == "0 B"
    assert ebl_storage.format_size(999) == "999 B"
    assert ebl_storage.format_size(1000) == "1 KB"
    assert ebl_storage.format_size(340_000_000) == "340 MB"
    assert ebl_storage.format_size(1_200_000_000) == "1.2 GB"
    assert ebl_storage.format_size(11_300_000_000) == "11.3 GB"


def test_describe_counts_and_sums(tmp_path: Path):
    _make(tmp_path, "a/1.ebl", 1_500_000)
    _make(tmp_path, "a/2.ebl", 500_000)

    assert ebl_storage.describe(tmp_path) == (2, "2 MB")


def test_delete_all_removes_the_files_and_the_folders_left_empty_but_not_the_root_or_other_files(tmp_path: Path):
    root = tmp_path / "Actisense"
    _make(root, "2025/EBL000001/000001_001.ebl", 1_000_000)
    _make(root, "2025/EBL000001/000001_002.ebl", 1_000_000)
    _make(root, "2026/EBL000002/000002_001.ebl", 500_000)
    keep = _make(root, "2026/readme.txt", 5)

    assert ebl_storage.delete_all(root) == (3, "2 MB")

    assert ebl_storage.ebl_files(root) == []
    assert keep.exists()
    assert not (root / "2025").exists()
    assert not (root / "2026" / "EBL000002").exists()
    assert (root / "2026").exists()  # still holds readme.txt
    assert root.exists()


def test_delete_all_on_an_empty_or_missing_folder_deletes_nothing(tmp_path: Path):
    assert ebl_storage.delete_all(tmp_path / "nope") == (0, "0 B")
    (tmp_path / "empty").mkdir()
    assert ebl_storage.delete_all(tmp_path / "empty") == (0, "0 B")
    assert (tmp_path / "empty").exists()
