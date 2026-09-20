from datetime import datetime, timedelta
from pathlib import Path

import os

import pytest

from nmea2log.cli import (
    _acquire_lock,
    _AlreadyRunningError,
    _release_lock,
    build_arg_parser,
    main,
)
from nmea2log.pipeline import (
    _collect_samples,
    _discover_ebl_files,
    _dominant_source_only,
    _filter_to_dominant_engine,
    _merge_array_by_source,
    _select_primary_gps_source,
)
import struct

from nmea2log.model import EngineSample, Frame, PositionFix, SogSample, TripFuelSample


def test_dominant_source_only_picks_largest_group():
    by_source = {
        10: [PositionFix(datetime(2026, 7, 15, 9, 0), 52.30, 4.90)] * 5,
        11: [PositionFix(datetime(2026, 7, 15, 9, 0), 52.31, 4.91)] * 3,
    }

    result = _dominant_source_only(by_source)

    assert result == by_source[10]


def test_dominant_source_only_empty():
    assert _dominant_source_only({}) == []


def test_merge_array_by_source_combines_across_calls():
    target: dict = {}
    _merge_array_by_source(target, {10: [1, 2], 11: [3]}, list)
    _merge_array_by_source(target, {10: [4], 12: [5]}, list)

    assert target == {10: [1, 2, 4], 11: [3], 12: [5]}


def test_dominant_source_only_after_merge_reflects_full_session():
    """Regression test for a real bug: with multiple files, the dominant source must be
    determined over the whole session, not per file -- otherwise a minority GPS source could
    randomly "win" in one of the files and still cause false jumps."""
    target: dict = {}
    # file 1: source 10 slightly in the minority
    _merge_array_by_source(target, {10: [1, 2], 11: [1, 2, 3]}, list)
    # file 2: source 10 clearly in the majority -> source 10 wins over the whole session
    _merge_array_by_source(target, {10: [3, 4, 5, 6, 7], 11: [4]}, list)

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


def test_config_file_with_all_upload_fields_enables_upload_without_a_separate_flag(tmp_path, monkeypatch):
    """upload is no longer a separate enabled=true/false setting to keep in sync with the other
    upload fields (asked for explicitly, to match the Android app's own settings: SettingsStore.
    isSftpConfigComplete just checks host/user/password/remote_path are all filled in, no separate
    toggle) -- a config file with host/user/remote_path/key_file all set, but no 'enabled' key at
    all, must still default args.upload to True."""
    config_path = tmp_path / "nmea2log.ini"
    key_file = tmp_path / "key"
    key_file.write_text("fake key", encoding="utf-8")
    config_path.write_text(
        "[nmea2log]\nno_geocode = true\n\n"
        f"[upload]\nhost = example.com\nuser = me\nremote_path = logbook.html\nkey_file = {key_file}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    args = build_arg_parser().parse_args([])

    assert args.upload is True


def test_config_file_missing_one_upload_field_leaves_upload_off(tmp_path, monkeypatch):
    """The same four-field completeness check as the Android app's isSftpConfigComplete -- a
    config file missing even one of host/user/remote_path/key_file must not enable --upload by
    default (there's nothing complete enough to actually connect with)."""
    config_path = tmp_path / "nmea2log.ini"
    config_path.write_text(
        "[nmea2log]\nno_geocode = true\n\n[upload]\nhost = example.com\nuser = me\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    args = build_arg_parser().parse_args([])

    assert args.upload is False


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


def test_ebl_dir_config_default_applies_when_not_given_on_the_command_line(tmp_path, monkeypatch):
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


def test_main_reports_a_clear_error_when_upload_rest_is_missing_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    exit_code = None
    try:
        main(["--upload-rest", "--ebl-dir", str(tmp_path)])
    except SystemExit as exc:
        exit_code = exc.code

    assert exit_code == 2
    assert "--upload-rest needs" in capsys.readouterr().err


def test_main_no_upload_flag_overrides_upload_and_upload_rest(tmp_path, monkeypatch, capsys):
    """Regression test for a real incident: a local test run still uploaded to the live site
    because nmea2log.ini enables upload by default -- --no-upload must force --upload and
    --upload-rest both off regardless of what those flags (or the config file) request, so a
    local test run can never touch the live site by accident."""
    monkeypatch.chdir(tmp_path)

    exit_code = None
    try:
        main(["--upload", "--upload-rest", "--no-upload", "--ebl-dir", str(tmp_path)])
    except SystemExit as exc:
        exit_code = exc.code

    # Got past the "--upload needs ..."/"--upload-rest needs ..." validation (which would fire
    # if either were still enabled, since none of the required upload settings are supplied
    # here) -- reaching the unrelated "no .ebl files" error instead proves both were switched off.
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--upload needs" not in err
    assert "--upload-rest needs" not in err
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
        "nmea2log.pipeline._collect_samples", lambda frames: fixed_samples
    )
    monkeypatch.setattr(
        "nmea2log.pipeline._iter_frames_for_path", lambda path, state: iter([])
    )


def test_main_prefers_rest_upload_over_sftp_when_both_are_configured(tmp_path, monkeypatch):
    """--upload-rest needs no SSH key on this machine (see its own help text), so it's preferred
    over SFTP whenever both happen to be configured at once -- proven here by giving main() both
    complete sets of settings and checking only the REST path actually ran."""
    ebl_path = tmp_path / "000000_000.ebl"
    ebl_path.write_bytes(b"x" * 100)
    _stub_one_trip_samples(monkeypatch)

    rest_calls = []
    sftp_calls = []
    monkeypatch.setattr(
        "nmea2log.cli.upload_via_rest",
        lambda html_content, **kw: rest_calls.append((html_content, kw)),
    )
    monkeypatch.setattr(
        "nmea2log.cli.upload_file", lambda local_path, **kw: sftp_calls.append((local_path, kw))
    )

    exit_code = main(
        [
            "--ebl-dir", str(tmp_path), "-o", str(tmp_path / "logbook.csv"), "--no-geocode",
            "--upload-rest", "--upload-rest-url", "https://example.org/wp-json/nmea2log/v1/logbook",
            "--upload-rest-user", "alice", "--upload-rest-app-password", "abcd efgh",
            "--upload", "--upload-host", "example.com", "--upload-user", "me",
            "--upload-remote-path", "logbook.html", "--upload-key-file", str(tmp_path / "key"),
        ]
    )

    assert exit_code == 0
    assert len(rest_calls) == 1
    assert rest_calls[0][1]["url"] == "https://example.org/wp-json/nmea2log/v1/logbook"
    assert sftp_calls == []


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

    monkeypatch.setattr("nmea2log.pipeline._collect_samples", fake_collect_samples)
    monkeypatch.setattr(
        "nmea2log.pipeline._iter_frames_for_path", lambda path, state: iter([])
    )

    cache_file = tmp_path / "cache.pkl"
    common_args = [
        "--ebl-dir", str(tmp_path), "-o", str(tmp_path / "logbook.csv"), "--no-geocode",
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
        "nmea2log.pipeline._collect_samples", lambda frames: fixed_samples
    )
    monkeypatch.setattr(
        "nmea2log.pipeline._iter_frames_for_path", lambda path, state: iter([])
    )
    output = tmp_path / "logbook.csv"
    main(["--ebl-dir", str(tmp_path), "-o", str(output), "--no-geocode", "--no-sample-cache", *extra_args])
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
    import nmea2log.cli as cli

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
    import nmea2log.cli as cli

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
    import nmea2log.cli as cli

    ebl_path = tmp_path / "000000_000.ebl"
    ebl_path.write_bytes(b"x" * 100)
    monkeypatch.setattr(cli, "_pid_is_running", lambda pid: True)
    (tmp_path / ".nmea2log.lock").write_text("4242", encoding="utf-8")

    with pytest.raises(SystemExit):
        main(["--ebl-dir", str(tmp_path), "-o", str(tmp_path / "logbook.csv"), "--no-geocode"])

    err = capsys.readouterr().err
    assert "another nmea2log run" in err
    assert "4242" in err


def test_main_releases_the_lock_after_finishing(tmp_path: Path, monkeypatch):
    output = _run_with_one_trip(tmp_path, monkeypatch)

    assert not (output.parent / ".nmea2log.lock").exists()


def _make_trip_cache_fixture(tmp_path: Path):
    """Five files, each a clean stationary/moving slice of a season with three real trips split
    across them:
        f0: stay0 (dep)              f1: move -> trip1         f2: stay1 (arr trip1 / dep trip2)
        f3: move -> trip2, stay2 (arr trip2 / dep trip3)        f4: move -> trip3, stay3 (arr trip3)
    A run over f0..f3 finds exactly 2 trips (trip1, trip2); adding f4 in a later run finds a 3rd
    (trip3) without changing the first two. Deliberately not split one-trip-per-file -- trip2's
    own data straddles f2/f3, which is what makes the "back up one file" margin in
    choose_resume_index() (trip_cache.py) actually matter for this test."""
    def _dt(minute: int) -> datetime:
        return datetime(2026, 7, 15, 8, 0, 0) + timedelta(minutes=minute)

    def _stay(minutes, lat, lon):
        fixes = [PositionFix(_dt(m), lat, lon) for m in minutes]
        sogs = [SogSample(_dt(m), 0.0) for m in minutes]
        return fixes, sogs

    def _move(minutes, lat0, lon0, lat1, lon1):
        minutes = list(minutes)
        fixes, sogs = [], []
        for i, m in enumerate(minutes):
            frac = i / (len(minutes) - 1)
            fixes.append(PositionFix(_dt(m), lat0 + (lat1 - lat0) * frac, lon0 + (lon1 - lon0) * frac))
            sogs.append(SogSample(_dt(m), 3.0))
        return fixes, sogs

    f0_fixes, f0_sogs = _stay(range(0, 12), 52.30, 4.90)
    f1_fixes, f1_sogs = _move(range(12, 42), 52.30, 4.90, 52.40, 4.95)
    f2_fixes, f2_sogs = _stay(range(42, 54), 52.40, 4.95)
    move2_fixes, move2_sogs = _move(range(54, 84), 52.40, 4.95, 52.50, 5.00)
    stay2_fixes, stay2_sogs = _stay(range(84, 96), 52.50, 5.00)
    f3_fixes, f3_sogs = move2_fixes + stay2_fixes, move2_sogs + stay2_sogs
    move3_fixes, move3_sogs = _move(range(96, 126), 52.50, 5.00, 52.60, 5.05)
    stay3_fixes, stay3_sogs = _stay(range(126, 138), 52.60, 5.05)
    f4_fixes, f4_sogs = move3_fixes + stay3_fixes, move3_sogs + stay3_sogs

    # Not written to disk here -- unlike when this project took --ebl-dir *and* explicit logfiles
    # (now removed, --ebl-dir only), a test that only wants a subset of these files visible for a
    # given main() call has to control exactly which ones exist under its --ebl-dir at that point;
    # see _write() below and each test's own use of it.
    paths = {name: tmp_path / f"{name}.ebl" for name in ("f0", "f1", "f2", "f3", "f4")}

    samples_by_name = {
        "f0": ({10: f0_fixes}, {10: f0_sogs}, [], [], {}, {}, {}, [], {}),
        "f1": ({10: f1_fixes}, {10: f1_sogs}, [], [], {}, {}, {}, [], {}),
        "f2": ({10: f2_fixes}, {10: f2_sogs}, [], [], {}, {}, {}, [], {}),
        "f3": ({10: f3_fixes}, {10: f3_sogs}, [], [], {}, {}, {}, [], {}),
        "f4": ({10: f4_fixes}, {10: f4_sogs}, [], [], {}, {}, {}, [], {}),
    }
    return paths, samples_by_name


def _write(*paths: Path) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 100)


def _stub_samples_by_path(monkeypatch, paths, samples_by_name, call_log):
    """Like _stub_one_trip_samples, but returns *different* fixed samples per .ebl path instead
    of the same ones for every file -- needed to test the trip cache's per-file skip logic, which
    only makes sense when different files carry different data."""
    path_to_name = {path.resolve(): name for name, path in paths.items()}

    def fake_iter_frames_for_path(path, state):
        return iter([path])  # frames are otherwise unused -- just a sentinel _collect_samples reads back

    def fake_collect_samples(frames):
        path = list(frames)[0]
        name = path_to_name[Path(path).resolve()]
        call_log.append(name)
        return samples_by_name[name]

    monkeypatch.setattr("nmea2log.pipeline._iter_frames_for_path", fake_iter_frames_for_path)
    monkeypatch.setattr("nmea2log.pipeline._collect_samples", fake_collect_samples)


def test_main_trip_cache_skips_decoding_already_settled_files_on_a_later_run(tmp_path, monkeypatch, capsys):
    """Integration test for the trip cache: a second run with a new file appended must not
    re-decode the file(s) whose trip(s) are already fully settled -- only the still-open last
    known trip's own window, plus whatever's genuinely new, gets decoded again."""
    paths, samples_by_name = _make_trip_cache_fixture(tmp_path)
    call_log: list = []
    _stub_samples_by_path(monkeypatch, paths, samples_by_name, call_log)

    common_args = [
        "--ebl-dir", str(tmp_path),
        "-o", str(tmp_path / "logbook.csv"), "--no-geocode", "--no-weather", "--no-marine",
        "--no-sample-cache", "--lock-radius-m", "-1",
        "--trip-cache-file", str(tmp_path / "trips.pkl"),
    ]
    _write(paths["f0"], paths["f1"], paths["f2"], paths["f3"])

    exit_code = main(common_args)

    assert exit_code == 0
    assert call_log == ["f0", "f1", "f2", "f3"]  # nothing cached yet -- everything decoded
    assert "[info] 2 trip(s) found" in capsys.readouterr().err

    call_log.clear()
    _write(paths["f4"])  # the "new file appended" this test is named for

    exit_code = main(common_args)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "f0" not in call_log  # fully part of the now-settled first trip -- never touched again
    assert "f4" in call_log  # genuinely new data is always decoded
    assert len(call_log) < 5  # a real saving happened, not just "decode everything anyway"
    assert "[cache] Reusing" in captured.err
    assert "already-settled trip(s)" in captured.err
    assert "[info] 3 trip(s) found" in captured.err  # trip1 (cached) + trip2 + trip3, none lost or duplicated


def _make_mid_transit_resume_fixture(tmp_path: Path):
    """Like _make_trip_cache_fixture, but g2 deliberately straddles a moving-to-stationary
    boundary partway through its own data (the last 3 minutes of trip1's transit, then stay1)
    instead of starting cleanly at the stay -- so choose_resume_index() picks g2 as the resume
    file (its own first sample is still before stay1 begins), and a plain resume from g2 alone
    starts mid-transit: g0/g1 (trip1's own departure stay and the bulk of its transit) are
    excluded, real distance is not the whole tail's own though, exercising the exact real-world
    bug this fixture is for.
        g0: stay0 (dep)         g1: most of the move -> trip1   g2: rest of the move, then stay1
        g3: move -> trip2, stay2                                 g4: move -> trip3, stay3
    """
    def _dt(minute: int) -> datetime:
        return datetime(2026, 7, 15, 8, 0, 0) + timedelta(minutes=minute)

    def _stay(minutes, lat, lon):
        fixes = [PositionFix(_dt(m), lat, lon) for m in minutes]
        sogs = [SogSample(_dt(m), 0.0) for m in minutes]
        return fixes, sogs

    def _move(minutes, lat0, lon0, lat1, lon1):
        minutes = list(minutes)
        fixes, sogs = [], []
        for i, m in enumerate(minutes):
            frac = i / (len(minutes) - 1)
            fixes.append(PositionFix(_dt(m), lat0 + (lat1 - lat0) * frac, lon0 + (lon1 - lon0) * frac))
            sogs.append(SogSample(_dt(m), 3.0))
        return fixes, sogs

    f0_fixes, f0_sogs = _stay(range(0, 12), 52.30, 4.90)
    # One continuous move (min 12..41, 30 points) from 52.30,4.90 to 52.40,4.95, split so g1 gets
    # all but the last 3 points and g2 gets those last 3 -- g2's own first sample (min 39) is
    # still mid-transit, 3 minutes before stay1 actually begins (min 42).
    move1_fixes, move1_sogs = _move(range(12, 42), 52.30, 4.90, 52.40, 4.95)
    f1_fixes, f1_sogs = move1_fixes[:-3], move1_sogs[:-3]
    tail_fixes, tail_sogs = move1_fixes[-3:], move1_sogs[-3:]
    stay1_fixes, stay1_sogs = _stay(range(42, 54), 52.40, 4.95)
    f2_fixes, f2_sogs = tail_fixes + stay1_fixes, tail_sogs + stay1_sogs
    move2_fixes, move2_sogs = _move(range(54, 84), 52.40, 4.95, 52.50, 5.00)
    stay2_fixes, stay2_sogs = _stay(range(84, 96), 52.50, 5.00)
    f3_fixes, f3_sogs = move2_fixes + stay2_fixes, move2_sogs + stay2_sogs
    move3_fixes, move3_sogs = _move(range(96, 126), 52.50, 5.00, 52.60, 5.05)
    stay3_fixes, stay3_sogs = _stay(range(126, 138), 52.60, 5.05)
    f4_fixes, f4_sogs = move3_fixes + stay3_fixes, move3_sogs + stay3_sogs

    # Not written to disk here -- see _make_trip_cache_fixture's own matching comment; each test
    # using this fixture controls exactly which files exist under its --ebl-dir via _write().
    paths = {name: tmp_path / f"{name}.ebl" for name in ("f0", "f1", "f2", "f3", "f4")}

    samples_by_name = {
        "f0": ({10: f0_fixes}, {10: f0_sogs}, [], [], {}, {}, {}, [], {}),
        "f1": ({10: f1_fixes}, {10: f1_sogs}, [], [], {}, {}, {}, [], {}),
        "f2": ({10: f2_fixes}, {10: f2_sogs}, [], [], {}, {}, {}, [], {}),
        "f3": ({10: f3_fixes}, {10: f3_sogs}, [], [], {}, {}, {}, [], {}),
        "f4": ({10: f4_fixes}, {10: f4_sogs}, [], [], {}, {}, {}, [], {}),
    }
    return paths, samples_by_name


def test_main_trip_cache_widens_the_window_when_it_resumed_mid_transit(tmp_path, monkeypatch, capsys):
    """Regression test for a real bug found on live data: choose_resume_index() can only pick
    resume points at file granularity, and a stay that begins partway through its own resume
    file (rather than right at its start) leaves the reprocessed window starting mid-transit --
    build_trips() then has no known stay to depart the still-open trip from, and reports it as
    starting outside the log file instead of folding correctly into the real trip. _run() must
    detect this (an "Unknown (start outside log file)" first trip in a resumed window) and widen
    by decoding one more file before trusting the result."""
    paths, samples_by_name = _make_mid_transit_resume_fixture(tmp_path)
    call_log: list = []
    _stub_samples_by_path(monkeypatch, paths, samples_by_name, call_log)

    common_args = [
        "--ebl-dir", str(tmp_path),
        "-o", str(tmp_path / "logbook.csv"), "--no-geocode", "--no-weather", "--no-marine",
        "--no-sample-cache", "--lock-radius-m", "-1", "--min-leg-distance-nm", "0.2",
        "--trip-cache-file", str(tmp_path / "trips.pkl"),
    ]
    _write(paths["f0"], paths["f1"], paths["f2"], paths["f3"])

    exit_code = main(common_args)

    assert exit_code == 0
    assert "[info] 2 trip(s) found" in capsys.readouterr().err

    call_log.clear()
    _write(paths["f4"])

    exit_code = main(common_args)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "started mid-transit" in captured.err  # the widening actually fired, not a fluke pass
    assert "[info] 3 trip(s) found" in captured.err  # still exactly 3 -- no duplicate, no phantom
    # No trip anywhere in the output reports the "start outside log file" fallback -- the widened
    # window found trip1's real departure stay instead of guessing.
    assert "Unknown (start outside log file)" not in captured.err
    html = (tmp_path / "logbook.html").read_text(encoding="utf-8")
    assert "Unknown (start outside log file)" not in html


def test_main_trip_cache_widen_retries_more_than_once_when_needed(tmp_path, monkeypatch, capsys):
    """Same scenario as above, but with the transit split so that even one widened file is still
    entirely "moving" with no stay in it at all -- the retry must keep widening (not give up
    after a single attempt) until it actually reaches trip1's real departure stay (g0)."""
    paths, samples_by_name = _make_mid_transit_resume_fixture(tmp_path)
    # Cut f1 down to nothing (an empty file) so the first widen attempt (which would normally
    # land on f1, all-moving) still finds no stay -- forcing a second widen, all the way to f0.
    samples_by_name["f1"] = ({10: []}, {10: []}, [], [], {}, {}, {}, [], {})

    call_log: list = []
    _stub_samples_by_path(monkeypatch, paths, samples_by_name, call_log)

    common_args = [
        "--ebl-dir", str(tmp_path),
        "-o", str(tmp_path / "logbook.csv"), "--no-geocode", "--no-weather", "--no-marine",
        "--no-sample-cache", "--lock-radius-m", "-1", "--min-leg-distance-nm", "0.2",
        "--trip-cache-file", str(tmp_path / "trips.pkl"),
    ]
    _write(paths["f0"], paths["f1"], paths["f2"], paths["f3"])
    main(common_args)
    capsys.readouterr()

    _write(paths["f4"])
    exit_code = main(common_args)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err.count("started mid-transit") >= 2  # widened more than once
    assert "[info] 3 trip(s) found" in captured.err
    assert "Unknown (start outside log file)" not in captured.err


def test_main_trip_cache_is_invalidated_by_a_changed_threshold(tmp_path, monkeypatch, capsys):
    """A cache built under one set of build_trips() parameters must never be served to a run with
    different ones -- see config_signature() in trip_cache.py. Proven here by a changed
    --speed-threshold-kn forcing every file to be decoded again, not just the usual recent
    window."""
    paths, samples_by_name = _make_trip_cache_fixture(tmp_path)
    call_log: list = []
    _stub_samples_by_path(monkeypatch, paths, samples_by_name, call_log)

    trip_cache_file = tmp_path / "trips.pkl"
    _write(paths["f0"], paths["f1"], paths["f2"], paths["f3"])
    main(
        ["--ebl-dir", str(tmp_path)]
        + ["-o", str(tmp_path / "logbook.csv"), "--no-geocode", "--no-weather", "--no-marine",
           "--no-sample-cache", "--lock-radius-m", "-1",
           "--trip-cache-file", str(trip_cache_file)]
    )
    call_log.clear()
    capsys.readouterr()

    exit_code = main(
        ["--ebl-dir", str(tmp_path)]
        + ["-o", str(tmp_path / "logbook.csv"), "--no-geocode", "--no-weather", "--no-marine",
           "--no-sample-cache", "--lock-radius-m", "-1",
           "--trip-cache-file", str(trip_cache_file), "--speed-threshold-kn", "0.8"]
    )

    assert exit_code == 0
    assert call_log == ["f0", "f1", "f2", "f3"]  # the changed threshold invalidated the cache entirely


def test_main_trip_cache_is_invalidated_by_a_trip_logic_version_bump(tmp_path, monkeypatch, capsys):
    """A pure change to build_trips()'s own logic (e.g. a different track/position computation)
    changes none of its explicit parameters, so config_signature() alone can't tell an old cache
    apart from a fresh one -- see TRIP_LOGIC_VERSION in tripbuilder.py, included in the signature
    specifically so a bump there still forces a full rebuild, exactly like a changed threshold
    does above. Found in practice: without this, a real device kept serving already-settled trips
    built under old track-drawing logic indefinitely after installing a fix, with no visible
    effect until those trips happened to age out of the cache on their own -- which, for most of
    a season's worth of trips, is never."""
    paths, samples_by_name = _make_trip_cache_fixture(tmp_path)
    call_log: list = []
    _stub_samples_by_path(monkeypatch, paths, samples_by_name, call_log)

    trip_cache_file = tmp_path / "trips.pkl"
    _write(paths["f0"], paths["f1"], paths["f2"], paths["f3"])
    main(
        ["--ebl-dir", str(tmp_path)]
        + ["-o", str(tmp_path / "logbook.csv"), "--no-geocode", "--no-weather", "--no-marine",
           "--no-sample-cache", "--lock-radius-m", "-1",
           "--trip-cache-file", str(trip_cache_file)]
    )
    call_log.clear()
    capsys.readouterr()

    monkeypatch.setattr("nmea2log.pipeline.TRIP_LOGIC_VERSION", -1)
    exit_code = main(
        ["--ebl-dir", str(tmp_path)]
        + ["-o", str(tmp_path / "logbook.csv"), "--no-geocode", "--no-weather", "--no-marine",
           "--no-sample-cache", "--lock-radius-m", "-1",
           "--trip-cache-file", str(trip_cache_file)]
    )

    assert exit_code == 0
    assert call_log == ["f0", "f1", "f2", "f3"]  # the version bump invalidated the cache entirely


def test_main_trip_cache_falls_back_when_the_resume_file_is_gone(tmp_path, monkeypatch, capsys):
    """If the file a cached run recorded as its resume point can no longer be found among the
    files --ebl-dir now discovers (moved, deleted, or --ebl-dir now points somewhere else), the
    cache must not be guessed at -- it's logged and the run falls back to treating every given
    file as needing to be decoded, exactly like there was no cache at all."""
    _, samples_by_name = _make_trip_cache_fixture(tmp_path)
    # A genuinely different --ebl-dir per run (rather than the flat paths _make_trip_cache_fixture
    # itself returns) -- run2 must see *only* f4, disjoint from run1's f0..f3, to exercise "the
    # resume file is nowhere to be found any more" rather than just "not decoded this time".
    run1_dir, run2_dir = tmp_path / "run1", tmp_path / "run2"
    paths = {name: run1_dir / f"{name}.ebl" for name in ("f0", "f1", "f2", "f3")}
    paths["f4"] = run2_dir / "f4.ebl"
    call_log: list = []
    _stub_samples_by_path(monkeypatch, paths, samples_by_name, call_log)

    trip_cache_file = tmp_path / "trips.pkl"
    _write(paths["f0"], paths["f1"], paths["f2"], paths["f3"])
    main(
        ["--ebl-dir", str(run1_dir)]
        + ["-o", str(tmp_path / "logbook.csv"), "--no-geocode", "--no-weather", "--no-marine",
           "--no-sample-cache", "--lock-radius-m", "-1",
           "--trip-cache-file", str(trip_cache_file)]
    )
    call_log.clear()
    capsys.readouterr()

    # A disjoint file set -- none of these were the run's own recorded resume file.
    _write(paths["f4"])
    main(
        ["--ebl-dir", str(run2_dir)]
        + ["-o", str(tmp_path / "logbook.csv"), "--no-geocode", "--no-weather", "--no-marine",
           "--no-sample-cache", "--lock-radius-m", "-1",
           "--trip-cache-file", str(trip_cache_file)]
    )

    assert call_log == ["f4"]  # still decoded -- fallback, not a skip based on a guess
    assert "resume file isn't among the given logfiles" in capsys.readouterr().err


_T0 = datetime(2026, 7, 30, 12, 0, 0)


def _position_frame(second: int, lat: float, source: int = 11) -> Frame:
    data = struct.pack("<ii", round(lat * 1e7), round(-4.17 * 1e7))
    return Frame(time=_T0 + timedelta(seconds=second), source=source, destination=255, priority=2, pgn=129025, data=data)


def _dop_frame(second: int, payload_hex: str, source: int = 11) -> Frame:
    return Frame(
        time=_T0 + timedelta(seconds=second), source=source, destination=255, priority=6, pgn=129539,
        data=bytes.fromhex(payload_hex),
    )


# Real payloads from a boat's GNSS receiver (see test_pgn_decode.py): normal / no fix at power-on / power-off.
_DOP_NORMAL = "9bd746006e00ff7f"
_DOP_NO_FIX = "04d7ac26ac26ff7f"
_DOP_POWER_OFF = "01ffff7fff7fff7f"


def test_collect_samples_ignores_positions_while_the_receiver_reports_no_fix():
    """Real incident: after a power-on the GNSS receiver first sends its last remembered position
    (HDOP 99, ~170 m from the boat's real position) and only converges on the real one ~100 s later."""
    frames = [
        _dop_frame(0, _DOP_NO_FIX),
        _position_frame(0, 47.83745),  # remembered, wrong position -- must be ignored
        _position_frame(1, 47.83746),
        _dop_frame(2, _DOP_NORMAL),
        _position_frame(2, 47.83868),  # real position once the receiver has a fix
    ]

    fixes_by_source = _collect_samples(frames)[0]

    assert [round(f.lat, 5) for f in fixes_by_source[11]] == [47.83868]


def test_collect_samples_ignores_the_last_positions_sent_as_the_receiver_loses_power():
    frames = [
        _dop_frame(0, _DOP_NORMAL),
        _position_frame(0, 47.83868),
        _dop_frame(2, _DOP_NORMAL),
        _position_frame(3, 47.83868),  # in the DOP interval that ends in "lost fix": dropped too
        _dop_frame(4, _DOP_POWER_OFF),
        _position_frame(4, 47.83745),  # the 173 m jump right at shutdown
    ]

    fixes_by_source = _collect_samples(frames)[0]

    assert [round(f.lat, 5) for f in fixes_by_source[11]] == [47.83868]


def test_collect_samples_ignores_positions_that_arrive_before_the_first_no_fix_message():
    """The very first positions after a power-on come before the first DOP message that says the
    receiver has no fix -- they must be judged by that message, not slip through as 'unknown'."""
    frames = [
        _position_frame(0, 47.83745),  # remembered, wrong position
        _dop_frame(0, _DOP_NO_FIX),
        _dop_frame(2, _DOP_NORMAL),
        _position_frame(3, 47.83868),
    ]

    fixes_by_source = _collect_samples(frames)[0]

    assert [round(f.lat, 5) for f in fixes_by_source[11]] == [47.83868]


def test_collect_samples_keeps_everything_when_no_dop_message_was_seen():
    frames = [_position_frame(0, 47.83868), _position_frame(1, 47.83869)]

    assert len(_collect_samples(frames)[0][11]) == 2


def test_collect_samples_does_not_discard_a_receiver_that_never_reports_a_real_solution():
    """A device that always fills this message with 'not available' must not lose every position."""
    frames = [_dop_frame(0, _DOP_POWER_OFF), _position_frame(0, 47.83868), _position_frame(1, 47.83869)]

    assert len(_collect_samples(frames)[0][11]) == 2


def test_collect_samples_tracks_each_gnss_source_separately():
    frames = [
        _dop_frame(0, _DOP_NO_FIX, source=11),
        _position_frame(0, 47.83745, source=11),  # ignored
        _position_frame(0, 47.83868, source=10),  # source 10 never reported a problem
    ]

    fixes_by_source = _collect_samples(frames)[0]

    assert 11 not in fixes_by_source
    assert len(fixes_by_source[10]) == 1
