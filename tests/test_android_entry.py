from datetime import datetime, timedelta
from pathlib import Path

from nmea2000processor import android_entry
from nmea2000processor.geocode import NoGeocoder
from nmea2000processor.marine import NoMarine
from nmea2000processor.model import PositionFix, SogSample
from nmea2000processor.weather import NoWeather


def _write_fake_ebl(tmp_path: Path, name: str = "000000_000.ebl") -> Path:
    path = tmp_path / name
    path.write_bytes(b"x" * 100)  # content doesn't matter -- parsing itself is stubbed out below
    return path


def _stub_one_trip_samples(monkeypatch):
    """Same 12min stationary -> 30min underway -> 12min stationary shape as test_cli.py's
    _run_with_one_trip, but patching android_entry's own imported names -- `from .cli import
    _collect_samples` copies the reference into android_entry's namespace at import time, so
    patching cli.py's attribute afterwards would not affect what android_entry actually calls."""

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
    monkeypatch.setattr(android_entry, "_collect_samples", lambda frames: fixed_samples)
    monkeypatch.setattr(android_entry, "_iter_frames_for_path", lambda path, state: iter([]))
    # run_pipeline() now uses real Geocoder/WeatherFetcher/MarineFetcher instances by default (see
    # its own doc comment) -- stubbed out here the same way test_cli.py's own tests always pass
    # --no-geocode/--no-weather/--no-marine, so a plain test run never makes a real network request
    # (Nominatim/Overpass/Open-Meteo).
    monkeypatch.setattr(android_entry, "Geocoder", lambda **kwargs: NoGeocoder())
    monkeypatch.setattr(android_entry, "WeatherFetcher", lambda **kwargs: NoWeather())
    monkeypatch.setattr(android_entry, "MarineFetcher", lambda **kwargs: NoMarine())


def test_run_pipeline_returns_error_for_no_ebl_paths(tmp_path):
    result = android_entry.run_pipeline(
        ebl_paths=[],
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
    )

    assert result == {"ok": False, "error": "No .ebl files given."}


def test_run_pipeline_returns_error_for_a_missing_file(tmp_path):
    result = android_entry.run_pipeline(
        ebl_paths=[str(tmp_path / "does_not_exist.ebl")],
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
    )

    assert result["ok"] is False
    assert "not found" in result["error"].lower()


def test_run_pipeline_returns_error_when_no_position_data(tmp_path, monkeypatch):
    ebl_path = _write_fake_ebl(tmp_path)
    empty_samples = ({}, {}, [], [], {}, {}, {}, [], {})
    monkeypatch.setattr(android_entry, "_collect_samples", lambda frames: empty_samples)
    monkeypatch.setattr(android_entry, "_iter_frames_for_path", lambda path, state: iter([]))

    result = android_entry.run_pipeline(
        ebl_paths=[str(ebl_path)],
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
    )

    assert result == {"ok": False, "error": "No position data (PGN 129025) found."}


def test_run_pipeline_stops_decoding_once_cancelled(tmp_path, monkeypatch):
    """Regression test for a real bug: closing the app (SyncState.cancelled = true, see
    MainActivity.closeAppAndCancelSync()) had no effect at all once decoding had started -- only
    the earlier download loop in _sync_from_w2k2() ever checked should_cancel(), so a long decode
    (1000+ files on a real archive) just kept running for minutes after the user thought they'd
    stopped it. should_cancel is checked once per file, before decoding it -- cancelling right
    after the first file must stop before the second one is ever touched."""
    ebl_a = _write_fake_ebl(tmp_path, "000000_000.ebl")
    ebl_b = _write_fake_ebl(tmp_path, "000000_001.ebl")
    _stub_one_trip_samples(monkeypatch)

    seen_paths = []
    real_iter_frames = android_entry._iter_frames_for_path

    def _tracking_iter_frames(path, state):
        seen_paths.append(path)
        return real_iter_frames(path, state)

    monkeypatch.setattr(android_entry, "_iter_frames_for_path", _tracking_iter_frames)

    calls = []

    def fake_should_cancel():
        calls.append(1)
        return len(calls) > 1  # allow the first file, cancel before the second

    result = android_entry.run_pipeline(
        ebl_paths=[str(ebl_a), str(ebl_b)],
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
        should_cancel=fake_should_cancel,
    )

    assert result == {"ok": False, "error": "Sync cancelled.", "cancelled": True}
    assert seen_paths == [Path(ebl_a)]


def test_run_pipeline_writes_html_and_reports_trip_count(tmp_path, monkeypatch):
    ebl_path = _write_fake_ebl(tmp_path)
    _stub_one_trip_samples(monkeypatch)
    html_path = tmp_path / "logbook.html"

    result = android_entry.run_pipeline(
        ebl_paths=[str(ebl_path)],
        output_html_path=str(html_path),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
    )

    assert result == {"ok": True, "trip_count": 1, "html_path": str(html_path)}
    assert html_path.exists()


def test_run_pipeline_enables_the_remarks_column(tmp_path, monkeypatch):
    """Regression test for a real bug found in practice: run_pipeline() built its args with
    build_arg_parser().parse_args([]), which -- unlike the desktop CLI -- never reads
    nmea2log.ini (there's no such file on the phone). Back when --remarks-api-url's default came
    from that ini file, this left every phone-built logbook without a remarks column at all, only
    ever a desktop-built one had it. Now that the URL is a fixed, built-in default (see
    _DEFAULT_REMARKS_API_URL in html_writer.py) rather than an opt-in ini setting, a bare
    parse_args([]) already carries it -- this just confirms that stays true end to end."""
    ebl_path = _write_fake_ebl(tmp_path)
    _stub_one_trip_samples(monkeypatch)
    html_path = tmp_path / "logbook.html"

    android_entry.run_pipeline(
        ebl_paths=[str(ebl_path)],
        output_html_path=str(html_path),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
    )

    html = html_path.read_text(encoding="utf-8")
    assert 'REMARKS_API_URL = "/wp-json/nmea2log/v1/remarks"' in html


def test_sync_from_w2k2_returns_error_when_no_host_found(tmp_path, monkeypatch):
    monkeypatch.setattr(android_entry.w2k2_download, "discover_w2k2", lambda subnet_prefix: None)

    result = android_entry.sync_from_w2k2(
        user="skipper",
        password="geheim",
        subnet_prefix="192.168.43.",
        download_dir=str(tmp_path / "Actisense"),
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
    )

    assert result["ok"] is False
    assert "192.168.43.0/24" in result["error"]


def test_sync_from_w2k2_returns_a_real_error_for_an_unanticipated_exception(tmp_path, monkeypatch):
    """Regression test: an exception type none of the specific except clauses catch (e.g. a
    garbled response body failing json.loads() with a plain ValueError, simulated directly here)
    must still come back as {"ok": False, "error": "..."} with a real message -- not propagate
    uncaught past this call (Kotlin's own generic catch would then have nothing but a bare
    exception, and Kotlin's result.error ends up null, which showSyncResult() has no better
    fallback for than a useless "onbekende fout", asked for explicitly to fix)."""
    monkeypatch.setattr(android_entry.w2k2_download, "discover_w2k2", lambda subnet_prefix: "http://10.0.0.5")

    def broken_make_session(host, config):
        raise ValueError("could not parse the login response")

    monkeypatch.setattr(android_entry.w2k2_download, "make_session", broken_make_session)

    result = android_entry.sync_from_w2k2(
        user="skipper",
        password="geheim",
        subnet_prefix="192.168.43.",
        download_dir=str(tmp_path / "Actisense"),
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
    )

    assert result["ok"] is False
    assert "could not parse the login response" in result["error"]


def test_sync_from_w2k2_returns_a_real_error_when_discover_w2k2_itself_raises(tmp_path, monkeypatch):
    """Regression test: discover_w2k2() used to sit outside the try block entirely, so anything it
    raised (found in practice on a memory-pressured device: its subnet-scan thread pool failing to
    start a worker thread) propagated uncaught past this call -- same "onbekende fout" symptom as
    test_sync_from_w2k2_returns_a_real_error_for_an_unanticipated_exception, just from a different,
    previously-unguarded call site."""

    def broken_discover(subnet_prefix):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(android_entry.w2k2_download, "discover_w2k2", broken_discover)

    result = android_entry.sync_from_w2k2(
        user="skipper",
        password="geheim",
        subnet_prefix="192.168.43.",
        download_dir=str(tmp_path / "Actisense"),
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
    )

    assert result["ok"] is False
    assert "can't start new thread" in result["error"]


def test_sync_from_w2k2_returns_a_real_error_when_run_pipeline_itself_raises(tmp_path, monkeypatch):
    """Regression test: the call to run_pipeline() (decode/build_trips/write_html_logbook) used to
    have no exception handling of its own at all -- found in practice, a real crash on real
    full-scale data late in a run (near the very last file). Unlike a normally-returned
    {"ok": False, "error": ...} dict, an exception here never even reaches showSyncResult() on the
    Kotlin side at all (MainActivity.runSync()'s own generic catch just sets plain status text),
    so this is a stricter regression guard than the "onbekende fout" dialog cases above: the
    caller must always get back a real dict, never an escaped exception."""
    monkeypatch.setattr(android_entry.w2k2_download, "discover_w2k2", lambda subnet_prefix: "http://10.0.0.5")

    class _FakeSession:
        def download_to(self, path, params, target, should_cancel=None):
            target.write_bytes(b"x" * 10)

    monkeypatch.setattr(android_entry.w2k2_download, "make_session", lambda host, config: _FakeSession())
    monkeypatch.setattr(android_entry.w2k2_download, "get_folders", lambda session: [])

    def broken_run_pipeline(**kwargs):
        raise ValueError("boom while building trips")

    monkeypatch.setattr(android_entry, "run_pipeline", broken_run_pipeline)

    result = android_entry.sync_from_w2k2(
        user="skipper",
        password="geheim",
        subnet_prefix="192.168.43.",
        download_dir=str(tmp_path / "Actisense"),
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
    )

    assert result["ok"] is False
    assert "boom while building trips" in result["error"]


def test_build_from_local_files_forwards_log_lines_to_the_callback(tmp_path, monkeypatch):
    """Regression test: this call path (see MainActivity.runOfflineBuild()) used to have no log
    sink wired up at all, so a real (not cache-hit) decode left the screen stuck on one static
    message instead of the same "...decoded X/Y" progress a normal sync already shows -- whatever
    run_pipeline() logs while this is wired up must reach the callback, and the sink must not be
    left forwarding forever afterward."""
    from nmea2000processor.log import log
    import nmea2000processor.log as log_module

    def fake_run_pipeline(**kwargs):
        log("[info] ...decoded 1/1 logfile(s) so far")
        return {"ok": True, "trip_count": 1, "html_path": str(tmp_path / "logbook.html")}

    monkeypatch.setattr(android_entry, "run_pipeline", fake_run_pipeline)

    lines = []

    class _Listener:
        def report(self, current, total, file_name):
            pass

        def isCancelled(self):
            return False

        def onLogLine(self, line):
            lines.append(line)

        def onDownloadComplete(self):
            pass

        def onResult(self, ok, error, cancelled, trip_count, html_path, downloaded_count):
            pass

    result = android_entry.build_from_local_files(
        ebl_paths=[str(tmp_path / "000000_000.ebl")],
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
        progress_callback=_Listener(),
    )

    assert result["ok"] is True
    assert any("decoded 1/1 logfile(s) so far" in line for line in lines)
    assert log_module._log_sink is None  # cleared after the call, not left forwarding forever


def test_sync_from_w2k2_forwards_log_lines_to_the_callback(tmp_path, monkeypatch):
    """onLogLine() should receive the exact same messages the desktop CLI prints (asked for
    explicitly, so the Android app doesn't need a separately-maintained set of status text) -- and
    the sink must be cleared afterward so it doesn't keep forwarding into a finished call."""
    import nmea2000processor.log as log_module

    monkeypatch.setattr(android_entry.w2k2_download, "discover_w2k2", lambda subnet_prefix: "http://10.0.0.5")

    class _FakeSession:
        def download_to(self, path, params, target, should_cancel=None):
            target.write_bytes(b"x" * 10)

    monkeypatch.setattr(android_entry.w2k2_download, "make_session", lambda host, config: _FakeSession())
    monkeypatch.setattr(android_entry.w2k2_download, "get_folders", lambda session: [])
    monkeypatch.setattr(android_entry, "run_pipeline", lambda **kwargs: {"ok": True, "trip_count": 0})

    lines = []

    class _Listener:
        def report(self, current, total, file_name):
            pass

        def isCancelled(self):
            return False

        def onLogLine(self, line):
            lines.append(line)

        def onDownloadComplete(self):
            pass

        def onResult(self, ok, error, cancelled, trip_count, html_path, downloaded_count):
            pass

    android_entry.sync_from_w2k2(
        user="skipper",
        password="geheim",
        subnet_prefix="192.168.43.",
        download_dir=str(tmp_path / "Actisense"),
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
        progress_callback=_Listener(),
    )

    assert any("[info] Found W2K-2 at http://10.0.0.5" in line for line in lines)
    assert log_module._log_sink is None  # cleared after the call, not left forwarding forever


def test_sync_from_w2k2_calls_on_download_complete_once_before_the_pipeline_runs(tmp_path, monkeypatch):
    """Lets Kotlin drop its own network-activity indicator (the foreground sync notification)
    once there's no more network I/O left in this call -- decode/build below is pure CPU (asked
    for explicitly: a "dataSync" foreground service has a real cumulative time budget on
    Android 15+, no reason to keep spending it during the CPU-only part)."""
    events = []

    monkeypatch.setattr(android_entry.w2k2_download, "discover_w2k2", lambda subnet_prefix: "http://10.0.0.5")

    class _FakeSession:
        def download_to(self, path, params, target, should_cancel=None):
            target.write_bytes(b"x" * 10)

    monkeypatch.setattr(android_entry.w2k2_download, "make_session", lambda host, config: _FakeSession())
    monkeypatch.setattr(android_entry.w2k2_download, "get_folders", lambda session: [])

    def fake_run_pipeline(**kwargs):
        events.append("run_pipeline")
        return {"ok": True, "trip_count": 0}

    monkeypatch.setattr(android_entry, "run_pipeline", fake_run_pipeline)

    class _Listener:
        def report(self, current, total, file_name):
            pass

        def isCancelled(self):
            return False

        def onLogLine(self, line):
            pass

        def onDownloadComplete(self):
            events.append("onDownloadComplete")

        def onResult(self, ok, error, cancelled, trip_count, html_path, downloaded_count):
            pass

    android_entry.sync_from_w2k2(
        user="skipper",
        password="geheim",
        subnet_prefix="192.168.43.",
        download_dir=str(tmp_path / "Actisense"),
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
        progress_callback=_Listener(),
    )

    assert events == ["onDownloadComplete", "run_pipeline"]


def test_sync_from_w2k2_downloads_then_runs_the_pipeline(tmp_path, monkeypatch):
    """End-to-end through sync_from_w2k2's own orchestration (folder/file iteration, download
    decisions) with a fake W2K-2 session, stubbing out only run_pipeline itself -- confirms the
    exact set of locally-present files after downloading is what gets handed to the pipeline."""

    file_sizes = {
        "000001_000.ebl": 10,
        # Above _MIN_ACTIVE_FILE_SIZE_BYTES -- this is the last file in the last folder, so
        # without this it would be skipped as "still growing" (see w2k2_download.py).
        "000001_001.ebl": 1_000_000,
    }

    class _FakeSession:
        def __init__(self):
            self.downloaded = []

        def download_to(self, path, params, target, should_cancel=None):
            self.downloaded.append(target)
            # download_file() now verifies the actual downloaded size against info["file_size"]
            # (asked for explicitly, after a real device serving a truncated file got silently
            # logged as a success) -- write exactly as many bytes as that file is supposed to be.
            target.write_bytes(b"x" * file_sizes[target.name])

    fake_session = _FakeSession()
    monkeypatch.setattr(android_entry.w2k2_download.time, "sleep", lambda s: None)
    monkeypatch.setattr(android_entry.w2k2_download, "discover_w2k2", lambda subnet_prefix: "http://10.0.0.5")
    monkeypatch.setattr(android_entry.w2k2_download, "make_session", lambda host, config: fake_session)
    monkeypatch.setattr(
        android_entry.w2k2_download, "get_folders", lambda session: [{"name": "EBL000001"}]
    )
    monkeypatch.setattr(
        android_entry.w2k2_download,
        "get_files",
        lambda session, folder: [
            {"file_name": "000001_000.ebl", "file_size": file_sizes["000001_000.ebl"], "file_time": 0},
            {"file_name": "000001_001.ebl", "file_size": file_sizes["000001_001.ebl"], "file_time": 0},
        ],
    )

    captured = {}

    def _fake_run_pipeline(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "trip_count": 3, "html_path": kwargs["output_html_path"]}

    monkeypatch.setattr(android_entry, "run_pipeline", _fake_run_pipeline)

    download_dir = tmp_path / "Actisense"
    result = android_entry.sync_from_w2k2(
        user="skipper",
        password="geheim",
        subnet_prefix="192.168.43.",
        download_dir=str(download_dir),
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
    )

    assert result["ok"] is True
    assert result["downloaded_count"] == 2
    assert len(fake_session.downloaded) == 2
    assert sorted(Path(p).name for p in captured["ebl_paths"]) == ["000001_000.ebl", "000001_001.ebl"]


def test_sync_from_w2k2_reports_progress_only_for_files_it_actually_fetches(tmp_path, monkeypatch):
    """progress_callback.report() is meant to drive a "N/M downloaded" UI -- it should count only
    files this run actually fetches, not ones already complete locally or skipped as still-growing
    (see w2k2_download.build_download_plan()'s to_download), and total must match that same set."""

    class _FakeSession:
        def download_to(self, path, params, target, should_cancel=None):
            target.write_bytes(b"x" * 10)

    monkeypatch.setattr(android_entry.w2k2_download.time, "sleep", lambda s: None)
    monkeypatch.setattr(android_entry.w2k2_download, "discover_w2k2", lambda subnet_prefix: "http://10.0.0.5")
    monkeypatch.setattr(android_entry.w2k2_download, "make_session", lambda host, config: _FakeSession())
    monkeypatch.setattr(android_entry.w2k2_download, "get_folders", lambda session: [{"name": "EBL000001"}])
    monkeypatch.setattr(
        android_entry.w2k2_download,
        "get_files",
        lambda session, folder: [
            {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
            {"file_name": "000001_001.ebl", "file_size": 1_000_000, "file_time": 0},
        ],
    )
    monkeypatch.setattr(android_entry, "run_pipeline", lambda **kwargs: {"ok": True, "trip_count": 0})

    calls = []

    class _Listener:
        def report(self, current, total, file_name):
            calls.append((current, total, file_name))

        def isCancelled(self):
            return False

        def onLogLine(self, line):
            pass

        def onDownloadComplete(self):
            pass

        def onResult(self, ok, error, cancelled, trip_count, html_path, downloaded_count):
            pass

    android_entry.sync_from_w2k2(
        user="skipper",
        password="geheim",
        subnet_prefix="192.168.43.",
        download_dir=str(tmp_path / "Actisense"),
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
        progress_callback=_Listener(),
    )

    assert calls == [(1, 2, "000001_000.ebl"), (2, 2, "000001_001.ebl")]


def test_sync_from_w2k2_stops_between_files_when_cancelled(tmp_path, monkeypatch):
    """Closing the app should stop downloading rather than let it keep going in the background
    (asked for explicitly) -- isCancelled() is checked before each file, so a cancelled sync
    leaves whatever was already fully downloaded in place and skips the rest, no partial file."""

    class _FakeSession:
        def download_to(self, path, params, target, should_cancel=None):
            target.write_bytes(b"x" * 10)

    monkeypatch.setattr(android_entry.w2k2_download.time, "sleep", lambda s: None)
    monkeypatch.setattr(android_entry.w2k2_download, "discover_w2k2", lambda subnet_prefix: "http://10.0.0.5")
    monkeypatch.setattr(android_entry.w2k2_download, "make_session", lambda host, config: _FakeSession())
    monkeypatch.setattr(android_entry.w2k2_download, "get_folders", lambda session: [{"name": "EBL000001"}])
    monkeypatch.setattr(
        android_entry.w2k2_download,
        "get_files",
        lambda session, folder: [
            {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
            {"file_name": "000001_001.ebl", "file_size": 1_000_000, "file_time": 0},
        ],
    )
    run_pipeline_calls = []
    monkeypatch.setattr(android_entry, "run_pipeline", lambda **kwargs: run_pipeline_calls.append(kwargs))

    class _CancelAfterFirstFile:
        """Cancelled becomes true only once the first file has actually been reported downloaded
        -- not tied to a specific isCancelled() call count, since should_cancel is now checked in
        several places (per folder in build_download_plan(), per file in download_file()) rather
        than just once per loop iteration."""

        def __init__(self):
            self.reports = []

        def report(self, current, total, file_name):
            self.reports.append((current, total, file_name))

        def isCancelled(self):
            return len(self.reports) >= 1

        def onLogLine(self, line):
            pass

        def onDownloadComplete(self):
            pass

        def onResult(self, ok, error, cancelled, trip_count, html_path, downloaded_count):
            pass

    controller = _CancelAfterFirstFile()
    result = android_entry.sync_from_w2k2(
        user="skipper",
        password="geheim",
        subnet_prefix="192.168.43.",
        download_dir=str(tmp_path / "Actisense"),
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
        progress_callback=controller,
    )

    assert result == {"ok": False, "error": "Sync cancelled.", "cancelled": True}
    assert controller.reports == [(1, 2, "000001_000.ebl")]  # only the first file was fetched
    assert run_pipeline_calls == []  # cancelled before the pipeline ever ran
    assert (tmp_path / "Actisense" / "EBL000001" / "000001_000.ebl").exists()
    assert not (tmp_path / "Actisense" / "EBL000001" / "000001_001.ebl").exists()


def test_sync_from_w2k2_returns_error_on_network_failure(tmp_path, monkeypatch):
    def _raise(host, config):
        raise ConnectionResetError("connection reset")

    monkeypatch.setattr(android_entry.w2k2_download, "discover_w2k2", lambda subnet_prefix: "http://10.0.0.5")
    monkeypatch.setattr(android_entry.w2k2_download, "make_session", _raise)

    result = android_entry.sync_from_w2k2(
        user="skipper",
        password="geheim",
        subnet_prefix="192.168.43.",
        download_dir=str(tmp_path / "Actisense"),
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
    )

    assert result["ok"] is False
    assert "Network error" in result["error"]


def test_sync_from_w2k2_reports_the_final_result_via_onresult_too(tmp_path, monkeypatch):
    """Regression test for the "onbekende fout" saga (see _report_result()'s own docstring):
    reading result.get(...) back out of the dict Chaquopy returns to Kotlin sometimes came back
    wrong, seemingly regardless of memory pressure -- reproduced both after a full sync and after
    an early cancellation. onResult() is called explicitly, with plain primitives, while still
    inside this same call -- verify it actually receives the real values, not just that the
    returned dict does (that was never in doubt)."""
    monkeypatch.setattr(android_entry.w2k2_download, "discover_w2k2", lambda subnet_prefix: "http://10.0.0.5")

    class _FakeSession:
        def download_to(self, path, params, target, should_cancel=None):
            target.write_bytes(b"x" * 10)

    monkeypatch.setattr(android_entry.w2k2_download, "make_session", lambda host, config: _FakeSession())
    monkeypatch.setattr(android_entry.w2k2_download, "get_folders", lambda session: [])
    monkeypatch.setattr(
        android_entry,
        "run_pipeline",
        lambda **kwargs: {"ok": True, "trip_count": 3, "html_path": "/fake/logbook.html"},
    )

    calls = []

    class _Listener:
        def report(self, current, total, file_name):
            pass

        def isCancelled(self):
            return False

        def onLogLine(self, line):
            pass

        def onDownloadComplete(self):
            pass

        def onResult(self, ok, error, cancelled, trip_count, html_path, downloaded_count):
            calls.append((ok, error, cancelled, trip_count, html_path, downloaded_count))

    android_entry.sync_from_w2k2(
        user="skipper",
        password="geheim",
        subnet_prefix="192.168.43.",
        download_dir=str(tmp_path / "Actisense"),
        output_html_path=str(tmp_path / "logbook.html"),
        sample_cache_path=str(tmp_path / "cache.pkl"),
        boat_name="Test Boat",
        mmsi="244123456",
        call_sign="PA1234",
        progress_callback=_Listener(),
    )

    assert len(calls) == 1
    ok, error, cancelled, trip_count, html_path, downloaded_count = calls[0]
    assert ok is True
    assert error is None
    assert cancelled is False
    assert trip_count == 3
    assert html_path == "/fake/logbook.html"
    assert downloaded_count == 0  # no files needed downloading in this scenario


def test_report_result_uses_minus_one_for_a_missing_trip_or_downloaded_count(tmp_path):
    """The dict's trip_count/downloaded_count are None when not applicable (e.g. a failed run,
    or build_from_local_files()'s own result never sets downloaded_count at all) -- -1 stands in
    for that on the onResult() side (see _report_result()'s own docstring for why: a plain Int
    rather than an Int? on the Kotlin end)."""
    calls = []

    class _Listener:
        def onResult(self, ok, error, cancelled, trip_count, html_path, downloaded_count):
            calls.append((ok, error, cancelled, trip_count, html_path, downloaded_count))

    android_entry._report_result(_Listener(), {"ok": False, "error": "No trips found."})

    assert calls == [(False, "No trips found.", False, -1, None, -1)]
