from pathlib import Path

from nmea2000processor.remarks import load_remarks, save_remarks


def test_load_remarks_missing_file_returns_empty_dict(tmp_path: Path):
    assert load_remarks(tmp_path / "does_not_exist.csv") == {}


def test_save_and_load_round_trip(tmp_path: Path):
    path = tmp_path / "remarks.csv"
    remarks = {"uid-1": "Great sail, dolphins near departure", "uid-2": "Motored the whole way"}
    labels = {"uid-1": "2026-07-30|Concarneau|Loctudy", "uid-2": "2026-07-31|Loctudy|Sainte-Marine"}

    save_remarks(remarks, labels, path)
    loaded = load_remarks(path)

    assert loaded == remarks


def test_save_remarks_includes_readable_columns_for_manual_editing(tmp_path: Path):
    path = tmp_path / "remarks.csv"
    save_remarks({"uid-1": "Nice trip"}, {"uid-1": "2026-07-30|Concarneau|Loctudy"}, path)

    content = path.read_text(encoding="utf-8-sig")

    assert "2026-07-30" in content
    assert "Concarneau" in content
    assert "Loctudy" in content
    assert "Nice trip" in content


def test_blank_remarks_are_ignored_on_load(tmp_path: Path):
    path = tmp_path / "remarks.csv"
    save_remarks({"uid-1": "Kept", "uid-2": ""}, {}, path)

    loaded = load_remarks(path)

    assert loaded == {"uid-1": "Kept"}
