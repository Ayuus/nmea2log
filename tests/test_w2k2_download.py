import socket
import urllib.request
from pathlib import Path

import pytest

from nmea2000processor.w2k2_download import (
    _looks_like_w2k2,
    _needs_download,
    discover_w2k2,
    download_file,
    load_config,
    main,
)


def test_load_config_from_ini(tmp_path: Path):
    config_path = tmp_path / "w2k2.ini"
    config_path.write_text(
        "[w2k2]\n"
        "user = skipper\n"
        "password = geheim\n"
        "download_dir = mijn_logs\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.user == "skipper"
    assert config.password == "geheim"
    assert config.download_dir == Path("mijn_logs")
    assert config.token is None


def test_load_config_missing_file_uses_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("W2K2_TOKEN", raising=False)
    monkeypatch.delenv("W2K2_USER", raising=False)
    monkeypatch.delenv("W2K2_PASS", raising=False)
    monkeypatch.delenv("W2K2_DOWNLOAD_DIR", raising=False)

    config = load_config(tmp_path / "does_not_exist.ini")

    assert config.download_dir == Path("Actisense")
    assert config.token is None
    assert config.user is None
    assert config.password is None


def test_load_config_env_vars_override_file(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "w2k2.ini"
    config_path.write_text("[w2k2]\nuser = from-file\n", encoding="utf-8")
    monkeypatch.setenv("W2K2_USER", "from-env-var")

    config = load_config(config_path)

    assert config.user == "from-env-var"


class _FakeConnection:
    """Stand-in for the object socket.create_connection() returns, used as a context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_looks_like_w2k2_false_when_port_closed(monkeypatch):
    def fake_create_connection(address, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    assert _looks_like_w2k2("10.0.0.5") is False


def test_looks_like_w2k2_true_when_port_open_and_body_matches(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda address, timeout=None: _FakeConnection())

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n=None):
            return b"<html><head><title>Actisense</title></head></html>"

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse())

    assert _looks_like_w2k2("10.0.0.5") is True


def test_looks_like_w2k2_false_when_port_open_but_body_doesnt_match(monkeypatch):
    """Regression-style check: an unrelated device (e.g. a router's own admin page) that happens
    to answer on port 80 must not be mistaken for the W2K-2 just because *something* is there."""
    monkeypatch.setattr(socket, "create_connection", lambda address, timeout=None: _FakeConnection())

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n=None):
            return b"<html><head><title>Some Router</title></head></html>"

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _FakeResponse())

    assert _looks_like_w2k2("10.0.0.5") is False


def test_discover_w2k2_returns_the_matching_host(monkeypatch):
    import nmea2000processor.w2k2_download as w2k2_download

    monkeypatch.setattr(w2k2_download, "_local_subnet_prefix", lambda: "10.0.0.")
    monkeypatch.setattr(w2k2_download, "_looks_like_w2k2", lambda ip: ip == "10.0.0.42")

    assert discover_w2k2() == "http://10.0.0.42"


def test_discover_w2k2_returns_none_when_nothing_on_the_subnet_matches(monkeypatch):
    import nmea2000processor.w2k2_download as w2k2_download

    monkeypatch.setattr(w2k2_download, "_local_subnet_prefix", lambda: "10.0.0.")
    monkeypatch.setattr(w2k2_download, "_looks_like_w2k2", lambda ip: False)

    assert discover_w2k2() is None


def test_main_reports_a_clear_error_when_no_w2k2_is_found_on_the_network(monkeypatch, tmp_path):
    config_path = tmp_path / "w2k2.ini"
    config_path.write_text("[w2k2]\nuser = skipper\npassword = geheim\n", encoding="utf-8")

    import nmea2000processor.w2k2_download as w2k2_download

    monkeypatch.setattr(w2k2_download, "discover_w2k2", lambda: None)

    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(config_path)])

    assert "Could not find a W2K-2" in str(exc_info.value)


def test_main_reports_a_clear_error_when_discovery_has_no_network_route(monkeypatch, tmp_path):
    """Regression test for a real crash: with no active network connection at all,
    discover_w2k2() -> _local_subnet_prefix() raises a raw OSError ("network unreachable") when
    it asks the OS for its own outbound-routing address -- that call happens before main()'s
    try/except, so it surfaced as a raw traceback instead of the same clean "[error] network: ..."
    message a connection failure during login/download already got."""
    config_path = tmp_path / "w2k2.ini"
    config_path.write_text("[w2k2]\nuser = skipper\npassword = geheim\n", encoding="utf-8")

    import nmea2000processor.w2k2_download as w2k2_download

    def fake_discover_w2k2():
        raise OSError("[WinError 10051] network is unreachable")

    monkeypatch.setattr(w2k2_download, "discover_w2k2", fake_discover_w2k2)

    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(config_path)])

    assert "network" in str(exc_info.value)


def test_main_reports_a_clear_error_on_timeout_instead_of_a_raw_traceback(monkeypatch, tmp_path):
    """Regression test for a real crash: a read timeout while logging in (e.g. the boat's wifi
    isn't reachable) surfaced as a raw Python traceback instead of the same clean "[error]
    network: ..." message a connection failure already got -- TimeoutError isn't a subclass of
    urllib.error.URLError, so it slipped past the except clause."""
    config_path = tmp_path / "w2k2.ini"
    config_path.write_text(
        "[w2k2]\nuser = skipper\npassword = geheim\n",
        encoding="utf-8",
    )

    import nmea2000processor.w2k2_download as w2k2_download

    monkeypatch.setattr(w2k2_download, "discover_w2k2", lambda: "http://10.164.231.101")

    def fake_urlopen(request, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(config_path)])

    assert "network" in str(exc_info.value)


def test_main_reports_a_clear_error_on_connection_reset_instead_of_a_raw_traceback(monkeypatch, tmp_path):
    """Regression test for a real crash: the boat's own wifi hotspot reset the connection mid-
    download of a file, which crashed the whole run with a raw traceback instead of the same
    clean "[error] network: ..." message a plain URLError already got -- ConnectionResetError
    isn't a subclass of urllib.error.URLError, so it slipped past the except clause (found in
    practice)."""
    config_path = tmp_path / "w2k2.ini"
    config_path.write_text(
        "[w2k2]\nuser = skipper\npassword = geheim\n",
        encoding="utf-8",
    )

    import nmea2000processor.w2k2_download as w2k2_download

    monkeypatch.setattr(w2k2_download, "discover_w2k2", lambda: "http://10.164.231.101")

    def fake_urlopen(request, timeout=None):
        raise ConnectionResetError("connection reset")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(config_path)])

    assert "network" in str(exc_info.value)


def test_download_file_retries_and_succeeds_after_a_transient_connection_reset(tmp_path, monkeypatch):
    """A single transient connection reset (see the regression test above) must not give up on
    the file immediately -- retrying a couple of times is what actually recovers from a boat wifi
    hiccup instead of leaving that file (and every one queued after it this run) undownloaded."""
    import nmea2000processor.w2k2_download as w2k2_download

    calls = []

    class _FlakySession:
        def download_to(self, path, params, target):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionResetError("connection reset")
            target.write_bytes(b"x" * 10)

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)

    download_file(
        _FlakySession(), tmp_path, "EBL000001",
        {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
    )

    assert len(calls) == 2
    assert (tmp_path / "EBL000001" / "000001_000.ebl").read_bytes() == b"x" * 10


def test_download_file_gives_up_after_exhausting_retries(tmp_path, monkeypatch):
    import nmea2000processor.w2k2_download as w2k2_download

    class _AlwaysFailsSession:
        def download_to(self, path, params, target):
            raise ConnectionResetError("connection reset")

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)

    with pytest.raises(ConnectionResetError):
        download_file(
            _AlwaysFailsSession(), tmp_path, "EBL000001",
            {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
        )


def test_download_file_skips_a_small_still_growing_file(tmp_path):
    """The active file's reported size climbs a bit on every poll while the W2K-2 is still
    writing to it -- fetching it while it's barely started just wastes a download+decode cycle on
    data that'll be superseded by a bigger snapshot next run anyway."""
    calls = []

    class _Session:
        def download_to(self, path, params, target):
            calls.append(1)

    download_file(
        _Session(), tmp_path, "EBL000001",
        {"file_name": "000001_005.ebl", "file_size": 1000, "file_time": 0},
        skip_if_growing=True,
    )

    assert calls == []
    assert not (tmp_path / "EBL000001" / "000001_005.ebl").exists()


def test_download_file_does_not_skip_a_small_file_when_not_marked_growing(tmp_path):
    """Only the very last file gets the size-based skip (see main()) -- a small file anywhere else
    is provably already closed out (a newer file exists after it) and must always be fetched."""
    calls = []

    class _Session:
        def download_to(self, path, params, target):
            calls.append(1)
            target.write_bytes(b"x" * 1000)

    download_file(
        _Session(), tmp_path, "EBL000001",
        {"file_name": "000001_003.ebl", "file_size": 1000, "file_time": 0},
    )

    assert calls == [1]


def test_needs_download_missing_file(tmp_path: Path):
    assert _needs_download(tmp_path / "nope.ebl", 1234) is True


def test_needs_download_incomplete_file(tmp_path: Path):
    target = tmp_path / "partial.ebl"
    target.write_bytes(b"1234")

    assert _needs_download(target, 100) is True


def test_needs_download_complete_file(tmp_path: Path):
    target = tmp_path / "complete.ebl"
    target.write_bytes(b"1234")

    assert _needs_download(target, 4) is False
