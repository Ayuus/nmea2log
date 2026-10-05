import os
import time
from pathlib import Path

from nmea2log import android_entry, logbook_state


def _setup(tmp_path: Path, trips: int = 5):
    html = tmp_path / "logbook.html"
    html.write_text("<html></html>")
    ebl = tmp_path / "a.ebl"
    ebl.write_bytes(b"x")
    old = time.time() - 100
    os.utime(ebl, (old, old))
    build_inputs = logbook_state.inputs("Sea Swallow", "244012345", "PA1234", 10.0)
    logbook_state.write(html, build_inputs, trips)
    return html, ebl, build_inputs


def test_a_logbook_built_from_these_inputs_is_current(tmp_path: Path):
    html, ebl, build_inputs = _setup(tmp_path)

    assert logbook_state.current_trip_count(str(html), [str(ebl)], build_inputs) == 5


def test_a_newer_ebl_file_makes_it_out_of_date(tmp_path: Path):
    html, ebl, build_inputs = _setup(tmp_path)
    future = time.time() + 100
    os.utime(ebl, (future, future))

    assert logbook_state.current_trip_count(str(html), [str(ebl)], build_inputs) is None


def test_a_changed_setting_makes_it_out_of_date(tmp_path: Path):
    html, ebl, _ = _setup(tmp_path)

    for changed in (
        logbook_state.inputs("Other Boat", "244012345", "PA1234", 10.0),
        logbook_state.inputs("Sea Swallow", "1", "PA1234", 10.0),
        logbook_state.inputs("Sea Swallow", "244012345", "XX", 10.0),
        logbook_state.inputs("Sea Swallow", "244012345", "PA1234", 5.0),
    ):
        assert logbook_state.current_trip_count(str(html), [str(ebl)], changed) is None


def test_different_code_makes_it_out_of_date(tmp_path: Path):
    html, ebl, build_inputs = _setup(tmp_path)

    assert logbook_state.current_trip_count(str(html), [str(ebl)], {**build_inputs, "code": "other"}) is None


def test_no_logbook_no_record_or_no_ebl_files_means_assemble(tmp_path: Path):
    html, ebl, build_inputs = _setup(tmp_path)

    assert logbook_state.current_trip_count(str(tmp_path / "missing.html"), [str(ebl)], build_inputs) is None
    assert logbook_state.current_trip_count(str(html), [], build_inputs) is None
    (tmp_path / logbook_state.SIDECAR_NAME).unlink()
    assert logbook_state.current_trip_count(str(html), [str(ebl)], build_inputs) is None


def test_publish_skips_the_build_when_the_logbook_is_current_and_builds_when_it_is_not(tmp_path: Path, monkeypatch):
    html, ebl, _ = _setup(tmp_path)
    built = []
    monkeypatch.setattr(android_entry, "run_pipeline", lambda **kwargs: built.append(1) or {"ok": True, "trip_count": 7})
    args = ([str(ebl)], str(html), str(tmp_path / "cache"), "Sea Swallow", "244012345", "PA1234")

    result = android_entry.build_from_local_files(*args, min_stop_minutes=10.0, skip_if_current=True)

    assert built == [] and result["ok"] is True and result["trip_count"] == 5 and result["html_path"] == str(html)

    android_entry.build_from_local_files(*args, min_stop_minutes=5.0, skip_if_current=True)  # a setting changed
    android_entry.build_from_local_files(*args, min_stop_minutes=10.0)  # not asked to skip
    assert built == [1, 1]
