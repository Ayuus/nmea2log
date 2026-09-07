from datetime import datetime, timedelta
from pathlib import Path

import os

import pytest

from nmea2000processor.cli import (
    _acquire_lock,
    _AlreadyRunningError,
    _discover_ebl_files,
    _dominant_source_only,
    _filter_to_dominant_engine,
    _merge_by_source,
    _release_lock,
    _select_primary_gps_source,
    build_arg_parser,
    main,
)
from nmea2000processor.model import EngineSample, PositionFix, SogSample, TripFuelSample


def test_dominant_source_only_picks_largest_group():
    by_source = {
        10: [PositionFix(datetime(2026, 7, 15, 9, 0), 52.30, 4.90)] * 5,
        11: [PositionFix(datetime(2026, 7, 15, 9, 0), 52.31, 4.91)] * 3,
    }

    result = _dominant_source_only(by_source)

    assert result == by_source[10]


def test_dominant_source_only_empty():
    assert _dominant_source_only({}) == []


def test_merge_by_source_combines_across_calls():
    target: dict = {}
    _merge_by_source(target, {10: [1, 2], 11: [3]})
    _merge_by_source(target, {10: [4], 12: [5]})

    assert target == {10: [1, 2, 4], 11: [3], 12: [5]}


def test_dominant_source_only_after_merge_reflects_full_session():
    """Regression test for a real bug: with multiple files, the dominant source must be
    determined over the whole session, not per file -- otherwise a minority GPS source could
    randomly "win" in one of the files and still cause false jumps."""
    target: dict = {}
    # file 1: source 10 slightly in the minority
    _merge_by_source(target, {10: [1, 2], 11: [1, 2, 3]})
    # file 2: source 10 clearly in the majority -> source 10 wins over the whole session
    _merge_by_source(target, {10: [3, 4, 5, 6, 7], 11: [4]})

    result = _dominant_source_only(target)

    assert result == [1, 2, 3, 4, 5, 6, 7]


def _fixes(n: int) -> list:
    return [PositionFix(datetime(2026, 7, 15, 9, 0), 52.30, 4.90)] * n


def _sogs(n: int) -> list:
    return [SogSample(datetime(2026, 7, 15, 9, 0), 3.0)] * n


def test_select_primary_gps_source_uses_sog_from_same_source():
    """Position and speed must come from the same physical source when that source sends both
    -- even if a different source happened to send more speed messages."""
    fixes_by_source = {10: _fixes(100), 11: _fixes(90)}
    sogs_by_source = {10: _sogs(5), 11: _sogs(50)}  # source 11 sends by far the most SOG

    fixes, sogs, primary = _select_primary_gps_source(fixes_by_source, sogs_by_source)

    assert primary == 10  # source 10 has the most position messages, so it leads
    assert fixes == fixes_by_source[10]
    assert sogs == sogs_by_source[10]  # not source 11, even though it sent more SOG


def test_select_primary_gps_source_falls_back_when_primary_has_no_sog():
    """If the chosen position source itself doesn't send speed, the code falls back to the
    speed source with the most messages (a different physical device, but better than nothing)."""
    fixes_by_source = {10: _fixes(100)}  # source 10 doesn't send SOG
    sogs_by_source = {14: _sogs(20)}

    fixes, sogs, primary = _select_primary_gps_source(fixes_by_source, sogs_by_source)

    assert primary == 10
    assert sogs == sogs_by_source[14]


def test_select_primary_gps_source_no_fixes():
    fixes, sogs, primary = _select_primary_gps_source({}, {10: _sogs(3)})

    assert fixes == []
    assert sogs == _sogs(3)
    assert primary is None


def test_config_file_sets_argparse_defaults(tmp_path, monkeypatch):
    config_path = tmp_path / "nmea2log.ini"
    config_path.write_text(
        "[nmea2log]\nmin_trip_distance_nm = 0.3\nutc_offset = 2\nno_geocode = true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    args = build_arg_parser().parse_args([])

    assert args.min_trip_distance_nm == 0.3
    assert args.utc_offset == 2.0
    assert args.no_geocode is True


def test_config_file_default_is_overridden_by_explicit_cli_arg(tmp_path, monkeypatch):
    config_path = tmp_path / "nmea2log.ini"
    config_path.write_text("[nmea2log]\nmin_trip_distance_nm = 0.3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    args = build_arg_parser().parse_args(["--min-trip-distance-nm", "0.9"])

    assert args.min_trip_distance_nm == 0.9


def test_config_file_backup_remote_path_alone_enables_backup_ebl(tmp_path, monkeypatch):
    """backup_ebl is no longer a separate on/off setting to keep in sync with backup_remote_path
    (asked for explicitly, to match the Android app's own settings: a non-blank remote path is
    enough) -- a config file with backup_remote_path set but no backup_ebl key at all must still
    default args.backup_ebl to True."""
    # [nmea2log] must be present (even empty of anything relevant here) -- _apply_config_defaults
    # only reads the [upload] section at all once the [nmea2log] one exists (found while writing
    # this test); every real nmea2log.ini always has both, so this isn't otherwise a factor.
    config_path = tmp_path / "nmea2log.ini"
    config_path.write_text(
        "[nmea2log]\nno_geocode = true\n\n[upload]\nbackup_remote_path = private/ebl-backup\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    args = build_arg_parser().parse_args([])

    assert args.backup_ebl is True
    assert args.backup_remote_path == "private/ebl-backup"


def test_config_file_no_backup_remote_path_leaves_backup_ebl_off(tmp_path, monkeypatch):
    config_path = tmp_path / "nmea2log.ini"
    config_path.write_text(
        "[nmea2log]\nno_geocode = true\n\n[upload]\nhost = example.com\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    args = build_arg_parser().parse_args([])

    assert args.backup_ebl is False


def test_filter_to_dominant_engine_keeps_only_largest_instance():
    engine_samples = (
        [EngineSample(datetime(2026, 7, 15, 9, 0), 0, 8.0, 3600) for _ in range(10)]
        + [EngineSample(datetime(2026, 7, 15, 9, 0), 1, 8.0, 3600) for _ in range(2)]
    )
    trip_fuel_samples = [
        TripFuelSample(datetime(2026, 7, 15, 9, 0), 0, 100.0),
        TripFuelSample(datetime(2026, 7, 15, 9, 0), 1, 50.0),
    ]

    filtered_engine, filtered_fuel, _filtered_rpm = _filter_to_dominant_engine(
        engine_samples, trip_fuel_samples, []
    )

    assert all(sample.instance == 0 for sample in filtered_engine)
    assert len(filtered_engine) == 10
    assert filtered_fuel == [trip_fuel_samples[0]]


def test_filter_to_dominant_engine_passthrough_when_already_single_instance():
    engine_samples = [EngineSample(datetime(2026, 7, 15, 9, 0), 0, 8.0, 3600)]
    trip_fuel_samples = [TripFuelSample(datetime(2026, 7, 15, 9, 0), 0, 100.0)]

    filtered_engine, filtered_fuel, _filtered_rpm = _filter_to_dominant_engine(
        engine_samples, trip_fuel_samples, []
    )

    assert filtered_engine == engine_samples
    assert filtered_fuel == trip_fuel_samples


def test_discover_ebl_files_finds_files_recursively(tmp_path: Path):
    (tmp_path / "EBL000000").mkdir()
    (tmp_path / "EBL000001").mkdir()
    (tmp_path / "EBL000000" / "000000_000.ebl").write_bytes(b"")
    (tmp_path / "EBL000001" / "000001_000.ebl").write_bytes(b"")
    (tmp_path / "EBL000000" / "readme.txt").write_text("not an ebl file")

    found = _discover_ebl_files(tmp_path)

    assert found == sorted(
        [tmp_path / "EBL000000" / "000000_000.ebl", tmp_path / "EBL000001" / "000001_000.ebl"]
    )


def test_discover_ebl_files_empty_dir(tmp_path: Path):
    assert _discover_ebl_files(tmp_path) == []


def test_ebl_dir_config_default_applies_when_no_logfiles_given(tmp_path, monkeypatch):
    config_path = tmp_path / "nmea2log.ini"
    config_path.write_text(f"[nmea2log]\nebl_dir = {tmp_path}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    args = build_arg_parser().parse_args([])

    assert args.ebl_dir == tmp_path


def test_main_reports_a_clear_error_when_ebl_dir_has_no_ebl_files(tmp_path, monkeypatch, capsys):
    # No -o is given, so the new nmea2log.log (see log.py's set_log_file) would otherwise land
    # next to the *default* logbook.csv -- this project's own real working directory -- instead
    # of somewhere test-isolated (found in practice: a stray nmea2log.log left behind by the test
    # suite in the repo root).
    monkeypatch.chdir(tmp_path)
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    exit_code = None
    try:
        main(["--ebl-dir", str(empty_dir)])
    except SystemExit as exc:
        exit_code = exc.code

    assert exit_code == 2
    assert "no .ebl files found" in capsys.readouterr().err


def test_main_prunes_the_log_file_using_log_retention_days(tmp_path, monkeypatch):
    """Regression test: nmea2log.log grew forever with nothing ever trimming it. --log-retention-
    days must actually reach set_log_file (see log.py), not just exist as an unused flag."""
    monkeypatch.chdir(tmp_path)
    log_path = tmp_path / "nmea2log.log"
    old_line = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S") + " too old, should be dropped\n"
    log_path.write_text(old_line, encoding="utf-8")
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    try:
        main(["--ebl-dir", str(empty_dir), "--log-retention-days", "1"])
    except SystemExit:
        pass  # the empty --ebl-dir errors out right after set_log_file runs -- irrelevant here

    assert "too old" not in log_path.read_text(encoding="utf-8")


def test_main_reports_a_clear_error_when_upload_is_missing_settings(tmp_path, monkeypatch, capsys):
    # Isolated from any real nmea2log.ini (e.g. this project's own, which has real [upload]
    # settings filled in) -- otherwise those would supply the "missing" settings as config
    # defaults and this test would stop testing what it claims to (found in practice).
    monkeypatch.chdir(tmp_path)

    exit_code = None
    try:
        main(["--upload", "--ebl-dir", str(tmp_path)])
    except SystemExit as exc:
        exit_code = exc.code

    assert exit_code == 2
    assert "--upload needs" in capsys.readouterr().err


def test_main_reports_a_clear_error_when_backup_ebl_is_missing_settings(tmp_path, monkeypatch, capsys):
    # Isolated from any real nmea2log.ini for the same reason as the --upload test above.
    monkeypatch.chdir(tmp_path)

    exit_code = None
    try:
        main(["--backup-ebl", "--ebl-dir", str(tmp_path)])
    except SystemExit as exc:
        exit_code = exc.code

    assert exit_code == 2
    assert "--backup-ebl needs" in capsys.readouterr().err


def test_main_no_upload_flag_overrides_upload_and_backup_ebl(tmp_path, monkeypatch, capsys):
    """Regression test for a real incident: a local test run still uploaded to the live site
    because nmea2log.ini enables upload by default -- --no-upload must force both --upload and
    --backup-ebl off regardless of what --upload/--backup-ebl (or the config file) request, so a
    local test run can never touch the live site by accident."""
    monkeypatch.chdir(tmp_path)

    exit_code = None
    try:
        main(["--upload", "--backup-ebl", "--no-upload", "--ebl-dir", str(tmp_path)])
    except SystemExit as exc:
        exit_code = exc.code

    # Got past the "--upload needs ..."/"--backup-ebl needs ..." validation (which would fire if
    # either were still enabled, since none of the required upload settings are supplied here) --
    # reaching the unrelated "no .ebl files" error instead proves both were switched off.
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--upload needs" not in err
    assert "--backup-ebl needs" not in err
    assert "no .ebl files found" in err


def _stub_one_trip_samples(monkeypatch):
    """Makes any .ebl path passed to main() decode into the same single real trip (12 min
    stationary -> 30 min underway -> 12 min stationary -- enough for build_trips to recognize
    one; a single static position fix doesn't), needed to get past main()'s own "no trips found"
    -> exit 1 for tests that don't otherwise care about trip data. See also _run_with_one_trip
    below, which this mirrors but without hardcoding a single fixed ebl path/main() call itself."""
    def _dt(minute):
        return datetime(2026, 7, 15, 8, 0, 0) + timedelta(minutes=minute)

    fixes, sogs = [], []
    for m in range(0, 12):
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))
    for i, m in enumerate(range(12, 42)):
        frac = i / 29
        fixes.append(PositionFix(_dt(m), 52.30 + 0.10 * frac, 4.90 + 0.05 * frac))
        sogs.append(SogSample(_dt(m), 3.0))
    for m in range(42, 54):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))

    fixed_samples = ({10: fixes}, {10: sogs}, [], [], {}, {}, {}, [], {})
    monkeypatch.setattr(
        "nmea2000processor.cli._collect_samples", lambda frames: fixed_samples
    )
    monkeypatch.setattr(
        "nmea2000processor.cli._iter_frames_for_path", lambda path, state: iter([])
    )


def test_main_backs_up_only_the_logfiles_not_already_on_the_server(tmp_path, monkeypatch):
    """Regression-style test for the whole point of --backup-ebl: a file already present on the
    server (per list_remote_filenames_multi) must not be uploaded again, so a run only ever sends
    what's new since the last one."""
    # Isolated from any real nmea2log.ini for the same reason as the --upload test above -- this
    # project's own [upload] section (enabled=true, a real remote_path) would otherwise supply
    # defaults that trigger a *real* (if doomed-to-fail) --upload attempt before ever reaching
    # the backup step this test is actually about (found in practice).
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "EBL000000"
    folder.mkdir()
    already_there = folder / "000000_000.ebl"
    new_file = folder / "000000_001.ebl"
    already_there.write_bytes(b"x" * 100)
    new_file.write_bytes(b"x" * 100)
    _stub_one_trip_samples(monkeypatch)

    listed_dirs = []
    uploaded = {}

    def fake_list_remote_filenames_multi(remote_dirs, *, host, user, key_file, port):
        listed_dirs.extend(remote_dirs)
        return {"000000_000.ebl"}

    def fake_upload_files_to_dirs(local_paths_by_remote_dir, *, host, user, key_file, port):
        uploaded["by_dir"] = local_paths_by_remote_dir

    monkeypatch.setattr("nmea2000processor.cli.list_remote_filenames_multi", fake_list_remote_filenames_multi)
    monkeypatch.setattr("nmea2000processor.cli.upload_files_to_dirs", fake_upload_files_to_dirs)

    exit_code = main(
        [
            str(already_there), str(new_file), "-o", str(tmp_path / "logbook.csv"), "--no-geocode",
            "--backup-ebl", "--backup-remote-path", "private/ebl-backup",
            "--upload-host", "example.com", "--upload-user", "me",
            "--upload-key-file", str(tmp_path / "key"),
        ]
    )

    assert exit_code == 0
    # Uploaded into a same-named remote subfolder (asked for explicitly), not the flat
    # backup_remote_path directly -- mirrors the local EBL000000/ layout on the server.
    assert listed_dirs == ["private/ebl-backup/EBL000000"]
    assert uploaded["by_dir"] == {"private/ebl-backup/EBL000000": [new_file]}


def test_main_backs_up_each_ebl_folder_into_its_own_remote_subfolder(tmp_path, monkeypatch):
    """Files from two different local EBL folders must land in two different remote subfolders --
    matches the Android app's own backup layout (asked for explicitly), instead of one flat remote
    directory for every folder's files combined. Both are still checked and uploaded together in
    a single session each (list_remote_filenames_multi/upload_files_to_dirs), not one session per
    folder -- see those functions' own docstrings for why that matters."""
    monkeypatch.chdir(tmp_path)
    folder_a = tmp_path / "EBL000000"
    folder_b = tmp_path / "EBL000007"
    folder_a.mkdir()
    folder_b.mkdir()
    file_a = folder_a / "000000_000.ebl"
    file_b = folder_b / "000007_000.ebl"
    file_a.write_bytes(b"x" * 100)
    file_b.write_bytes(b"x" * 100)
    _stub_one_trip_samples(monkeypatch)

    listed_dirs = []
    uploaded = {}

    def fake_list_remote_filenames_multi(remote_dirs, *, host, user, key_file, port):
        listed_dirs.extend(remote_dirs)
        return set()

    def fake_upload_files_to_dirs(local_paths_by_remote_dir, *, host, user, key_file, port):
        uploaded["by_dir"] = local_paths_by_remote_dir

    monkeypatch.setattr("nmea2000processor.cli.list_remote_filenames_multi", fake_list_remote_filenames_multi)
    monkeypatch.setattr("nmea2000processor.cli.upload_files_to_dirs", fake_upload_files_to_dirs)

    exit_code = main(
        [
            str(file_a), str(file_b), "-o", str(tmp_path / "logbook.csv"), "--no-geocode",
            "--backup-ebl", "--backup-remote-path", "private/ebl-backup",
            "--upload-host", "example.com", "--upload-user", "me",
            "--upload-key-file", str(tmp_path / "key"),
        ]
    )

    assert exit_code == 0
    # both folders checked together, in one call -- not one call per folder
    assert sorted(listed_dirs) == ["private/ebl-backup/EBL000000", "private/ebl-backup/EBL000007"]
    assert uploaded["by_dir"] == {
        "private/ebl-backup/EBL000000": [file_a],
        "private/ebl-backup/EBL000007": [file_b],
    }


def test_main_backs_up_new_logfiles_in_chunks(tmp_path, monkeypatch):
    """Regression test for a real failure: a single SFTP session uploading everything in one go
    (1000+ files, several MB each, on a first-ever backup run) got its connection reset partway
    through by a shared-hosting server, losing whatever hadn't already landed. Uploading in
    smaller chunks -- each its own session -- means a reset loses less progress at once."""
    monkeypatch.chdir(tmp_path)
    from nmea2000processor.cli import _BACKUP_CHUNK_SIZE

    count = _BACKUP_CHUNK_SIZE + 3  # deliberately not an exact multiple of the chunk size
    files = [tmp_path / f"{i:06d}_000.ebl" for i in range(count)]
    for f in files:
        f.write_bytes(b"x" * 100)
    _stub_one_trip_samples(monkeypatch)

    monkeypatch.setattr(
        "nmea2000processor.cli.list_remote_filenames_multi",
        lambda remote_dirs, **kw: set(),
    )
    chunks = []
    monkeypatch.setattr(
        "nmea2000processor.cli.upload_files_to_dirs",
        lambda local_paths_by_remote_dir, **kw: chunks.append(
            [p for paths in local_paths_by_remote_dir.values() for p in paths]
        ),
    )

    exit_code = main(
        [
            *[str(f) for f in files], "-o", str(tmp_path / "logbook.csv"), "--no-geocode",
            "--backup-ebl", "--backup-remote-path", "private/ebl-backup",
            "--upload-host", "example.com", "--upload-user", "me",
            "--upload-key-file", str(tmp_path / "key"),
        ]
    )

    assert exit_code == 0
    assert [len(c) for c in chunks] == [_BACKUP_CHUNK_SIZE, 3]
    assert [f for chunk in chunks for f in chunk] == files


def test_main_does_not_fail_the_run_when_the_backup_upload_breaks_partway(tmp_path, monkeypatch, capsys):
    """Regression test for a real failure: a run whose logbook was already written and uploaded
    fine still reported the whole run as failed (exit code 1) just because the *backup*, a bonus
    step, didn't fully finish -- even though it's fully resumable next run (see
    list_remote_filenames_multi) and nothing about today's actual logbook was at risk."""
    monkeypatch.chdir(tmp_path)
    from nmea2000processor.cli import _BACKUP_CHUNK_SIZE
    from nmea2000processor.upload import UploadError

    count = _BACKUP_CHUNK_SIZE + 3
    files = [tmp_path / f"{i:06d}_000.ebl" for i in range(count)]
    for f in files:
        f.write_bytes(b"x" * 100)
    _stub_one_trip_samples(monkeypatch)

    monkeypatch.setattr("nmea2000processor.cli.list_remote_filenames_multi", lambda remote_dirs, **kw: set())

    call_count = 0

    def fake_upload_files_to_dirs(local_paths_by_remote_dir, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise UploadError("Connection reset")

    monkeypatch.setattr("nmea2000processor.cli.upload_files_to_dirs", fake_upload_files_to_dirs)

    exit_code = main(
        [
            *[str(f) for f in files], "-o", str(tmp_path / "logbook.csv"), "--no-geocode",
            "--backup-ebl", "--backup-remote-path", "private/ebl-backup",
            "--upload-host", "example.com", "--upload-user", "me",
            "--upload-key-file", str(tmp_path / "key"),
        ]
    )

    assert exit_code == 0  # not fatal to the run
    assert call_count == 2  # stopped after the failing chunk, didn't retry or skip ahead
    assert "stopped after 25 new file(s)" in capsys.readouterr().err


def test_main_reuses_cached_samples_on_a_second_run(tmp_path, monkeypatch, capsys):
    """Integration test for the .ebl sample cache: a second run against the same, unchanged file
    must not re-parse it (only reuse the decoded samples from the first run's cache)."""
    ebl_path = tmp_path / "000000_000.ebl"
    ebl_path.write_bytes(b"x" * 100)  # content doesn't matter -- parsing itself is stubbed out

    call_count = 0
    fixed_samples = (
        {10: [PositionFix(datetime(2026, 7, 15, 9, 0), 52.30, 4.90)]},
        {10: [SogSample(datetime(2026, 7, 15, 9, 0), 0.0)]},
        [],
        [],
        {},
        {},
        {},
        [],
        {},
    )

    def fake_collect_samples(frames):
        nonlocal call_count
        call_count += 1
        return fixed_samples

    monkeypatch.setattr("nmea2000processor.cli._collect_samples", fake_collect_samples)
    monkeypatch.setattr(
        "nmea2000processor.cli._iter_frames_for_path", lambda path, state: iter([])
    )

    cache_file = tmp_path / "cache.pkl"
    common_args = [
        str(ebl_path), "-o", str(tmp_path / "logbook.csv"), "--no-geocode",
        "--sample-cache-file", str(cache_file),
    ]

    main(common_args)
    assert call_count == 1

    capsys.readouterr()  # discard first run's captured output
    main(common_args)

    assert call_count == 1  # second run must be served from cache, not re-parsed
    captured = capsys.readouterr()
    assert "[cache] reused decoded samples for 1/1 file(s)" in captured.err


def _run_with_one_trip(tmp_path: Path, monkeypatch, extra_args=()):
    """12 min stationary -> 30 min underway -> 12 min stationary, enough for build_trips to
    recognize one real trip (a single position fix doesn't -- there's no departure/arrival to
    tell apart)."""
    ebl_path = tmp_path / "000000_000.ebl"
    ebl_path.write_bytes(b"x" * 100)  # content doesn't matter -- parsing itself is stubbed out

    def _dt(minute):
        return datetime(2026, 7, 15, 8, 0, 0) + timedelta(minutes=minute)

    fixes, sogs = [], []
    for m in range(0, 12):
        fixes.append(PositionFix(_dt(m), 52.30, 4.90))
        sogs.append(SogSample(_dt(m), 0.0))
    for i, m in enumerate(range(12, 42)):
        frac = i / 29
        fixes.append(PositionFix(_dt(m), 52.30 + 0.10 * frac, 4.90 + 0.05 * frac))
        sogs.append(SogSample(_dt(m), 3.0))
    for m in range(42, 54):
        fixes.append(PositionFix(_dt(m), 52.40, 4.95))
        sogs.append(SogSample(_dt(m), 0.0))

    fixed_samples = (
        {10: fixes},
        {10: sogs},
        [],
        [],
        {},
        {},
        {},
        [],
        {},
    )
    monkeypatch.setattr(
        "nmea2000processor.cli._collect_samples", lambda frames: fixed_samples
    )
    monkeypatch.setattr(
        "nmea2000processor.cli._iter_frames_for_path", lambda path, state: iter([])
    )
    output = tmp_path / "logbook.csv"
    main([str(ebl_path), "-o", str(output), "--no-geocode", "--no-sample-cache", *extra_args])
    return output


def test_main_only_writes_html_by_default(tmp_path: Path, monkeypatch):
    """Regression test: CSV and GPX used to be written unconditionally on every run, which most
    of the time nobody looks at -- now they're opt-in via --csv/--gpx, default is HTML only."""
    output = _run_with_one_trip(tmp_path, monkeypatch)

    assert not output.exists()
    assert not output.with_suffix(".gpx").exists()
    assert output.with_suffix(".html").exists()


def test_main_writes_csv_and_gpx_when_requested(tmp_path: Path, monkeypatch):
    output = _run_with_one_trip(tmp_path, monkeypatch, extra_args=["--csv", "--gpx"])

    assert output.exists()
    assert output.with_suffix(".gpx").exists()
    assert output.with_suffix(".html").exists()


def test_acquire_lock_creates_a_lock_file_with_the_current_pid(tmp_path: Path):
    lock_path = tmp_path / ".nmea2log.lock"

    _acquire_lock(lock_path)

    assert lock_path.exists()
    assert int(lock_path.read_text(encoding="utf-8")) == os.getpid()


def test_acquire_lock_raises_when_another_process_still_holds_it(tmp_path: Path, monkeypatch):
    """Regression test for the whole point of the lock: two nmea2log runs against the same
    output directory race on the shared sample cache and HTML output -- found in practice, a
    re-launched run while the first was still working silently produced a live logbook with only
    1 of 15 real trips instead of a clear error."""
    import nmea2000processor.cli as cli

    lock_path = tmp_path / ".nmea2log.lock"
    lock_path.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cli, "_pid_is_running", lambda pid: pid == 4242)

    with pytest.raises(_AlreadyRunningError) as exc_info:
        _acquire_lock(lock_path)

    assert exc_info.value.pid == 4242
    assert lock_path.read_text(encoding="utf-8") == "4242"  # untouched, not stolen


def test_acquire_lock_takes_over_a_stale_lock(tmp_path: Path, monkeypatch):
    """A lock file left behind by a run that crashed or was killed without cleaning up must not
    permanently block every future run -- its PID no longer being alive is what tells the
    difference from a run that's still genuinely in progress."""
    import nmea2000processor.cli as cli

    lock_path = tmp_path / ".nmea2log.lock"
    lock_path.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(cli, "_pid_is_running", lambda pid: False)

    _acquire_lock(lock_path)

    assert int(lock_path.read_text(encoding="utf-8")) == os.getpid()


def test_acquire_lock_takes_over_an_unparseable_lock_file(tmp_path: Path):
    """A corrupted/empty lock file (e.g. the process was killed mid-write) must not permanently
    block every future run either -- same reasoning as a stale PID, just a different way for a
    leftover lock to be unreadable rather than genuinely still held."""
    lock_path = tmp_path / ".nmea2log.lock"
    lock_path.write_text("not a pid", encoding="utf-8")

    _acquire_lock(lock_path)

    assert int(lock_path.read_text(encoding="utf-8")) == os.getpid()


def test_release_lock_removes_the_file(tmp_path: Path):
    lock_path = tmp_path / ".nmea2log.lock"
    _acquire_lock(lock_path)

    _release_lock(lock_path)

    assert not lock_path.exists()


def test_release_lock_is_a_noop_if_the_file_is_already_gone(tmp_path: Path):
    lock_path = tmp_path / ".nmea2log.lock"

    _release_lock(lock_path)  # must not raise


def test_main_refuses_to_run_while_another_instance_holds_the_lock(tmp_path: Path, monkeypatch, capsys):
    import nmea2000processor.cli as cli

    ebl_path = tmp_path / "000000_000.ebl"
    ebl_path.write_bytes(b"x" * 100)
    monkeypatch.setattr(cli, "_pid_is_running", lambda pid: True)
    (tmp_path / ".nmea2log.lock").write_text("4242", encoding="utf-8")

    with pytest.raises(SystemExit):
        main([str(ebl_path), "-o", str(tmp_path / "logbook.csv"), "--no-geocode"])

    err = capsys.readouterr().err
    assert "another nmea2log run" in err
    assert "4242" in err


def test_main_releases_the_lock_after_finishing(tmp_path: Path, monkeypatch):
    output = _run_with_one_trip(tmp_path, monkeypatch)

    assert not (output.parent / ".nmea2log.lock").exists()
