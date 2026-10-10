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


def test_backup_copies_the_archive_with_its_folders_and_only_what_is_new(tmp_path: Path):
    source = tmp_path / "phone" / "Actisense"
    _make(source, "2025/EBL000001/000001_001.ebl", 100)
    _make(source, "2025/EBL000001/000001_002.ebl", 200)
    _make(source, "2025/notes.txt", 5)
    stick = tmp_path / "stick"
    stick.mkdir()
    seen = []

    result = ebl_storage.backup_to(source, stick, lambda current, total, name: seen.append((current, total, name)))

    assert result == {"copied": 2, "skipped": 0, "errors": [], "size": "300 B"}
    assert sorted(p.relative_to(stick).as_posix() for p in stick.rglob("*") if p.is_file()) == [
        "Actisense/2025/EBL000001/000001_001.ebl",
        "Actisense/2025/EBL000001/000001_002.ebl",
    ]
    assert seen == [(1, 2, "000001_001.ebl"), (2, 2, "000001_002.ebl")]

    _make(source, "2025/EBL000001/000001_003.ebl", 50)
    second = ebl_storage.backup_to(source, stick)

    assert second == {"copied": 1, "skipped": 2, "errors": [], "size": "50 B"}


def test_backup_replaces_a_file_that_is_there_with_another_size(tmp_path: Path):
    source = tmp_path / "phone" / "Actisense"
    _make(source, "EBL000001/000001_001.ebl", 100)
    stick = tmp_path / "stick"
    _make(stick, "Actisense/EBL000001/000001_001.ebl", 40)  # an earlier, cut-off copy

    result = ebl_storage.backup_to(source, stick)

    assert result["copied"] == 1
    assert (stick / "Actisense" / "EBL000001" / "000001_001.ebl").stat().st_size == 100


def test_backup_can_be_imported_again_as_it_is(tmp_path: Path):
    from nmea2log import import_ebl

    source = tmp_path / "phone" / "Actisense"
    _make(source, "EBL000001/000001_001.ebl", 100)
    stick = tmp_path / "stick"
    stick.mkdir()
    ebl_storage.backup_to(source, stick)

    new_phone = tmp_path / "new-phone"
    result = import_ebl.import_staged_ebl_files(
        [str(p) for p in ebl_storage.ebl_files(stick / "Actisense")], str(new_phone)
    )

    assert result["imported"] == 1 and result["errors"] == []


def test_backup_leaves_no_part_file_and_records_a_file_that_cannot_be_written(tmp_path: Path, monkeypatch):
    source = tmp_path / "phone" / "Actisense"
    _make(source, "EBL000001/000001_001.ebl", 100)
    _make(source, "EBL000001/000001_002.ebl", 100)
    stick = tmp_path / "stick"
    stick.mkdir()
    real_copy = ebl_storage.shutil.copyfile

    def failing_copy(src, dst, *args, **kwargs):
        if str(src).endswith("000001_001.ebl"):
            raise OSError("No space left on device")
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(ebl_storage.shutil, "copyfile", failing_copy)

    result = ebl_storage.backup_to(source, stick)

    assert result["copied"] == 1 and result["errors"] == ["000001_001.ebl"]
    assert not list(stick.rglob("*.part"))


def test_backup_of_a_missing_folder_is_nothing(tmp_path: Path):
    assert ebl_storage.backup_to(tmp_path / "nope", tmp_path) == {"copied": 0, "skipped": 0, "errors": [], "size": "0 B"}
