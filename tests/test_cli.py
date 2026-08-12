from datetime import datetime, timedelta
from pathlib import Path

from nmea2000processor.cli import (
    _discover_ebl_files,
    _dominant_source_only,
    _filter_to_dominant_engine,
    _merge_by_source,
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


def test_main_reports_a_clear_error_when_ebl_dir_has_no_ebl_files(tmp_path, capsys):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    exit_code = None
    try:
        main(["--ebl-dir", str(empty_dir)])
    except SystemExit as exc:
        exit_code = exc.code

    assert exit_code == 2
    assert "no .ebl files found" in capsys.readouterr().err


def test_main_reports_a_clear_error_when_upload_is_missing_settings(tmp_path, capsys):
    exit_code = None
    try:
        main(["--upload", "--ebl-dir", str(tmp_path)])
    except SystemExit as exc:
        exit_code = exc.code

    assert exit_code == 2
    assert "--upload needs" in capsys.readouterr().err


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

    def fake_collect_samples(frames, deadline=None):
        nonlocal call_count
        call_count += 1
        return fixed_samples

    monkeypatch.setattr("nmea2000processor.cli._collect_samples", fake_collect_samples)
    monkeypatch.setattr(
        "nmea2000processor.cli._iter_frames_for_path", lambda path, start_date, state: iter([])
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
        "nmea2000processor.cli._collect_samples", lambda frames, deadline=None: fixed_samples
    )
    monkeypatch.setattr(
        "nmea2000processor.cli._iter_frames_for_path", lambda path, start_date, state: iter([])
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
