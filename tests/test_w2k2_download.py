import json
import socket
import urllib.request
from pathlib import Path

import pytest

from nmea2000processor.w2k2_download import (
    _candidate_subnet_prefixes,
    _local_subnet_prefix,
    _looks_like_w2k2,
    _needs_download,
    _will_download,
    _windows_private_subnet_prefixes,
    build_download_plan,
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


class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def test_windows_private_subnet_prefixes_orders_real_adapters_before_virtual_ones(monkeypatch):
    """Regression test for a real bug found in practice, on a real dev machine: a Hyper-V virtual
    switch adapter (172.23.192.1) was up alongside the real wifi (172.16.121.93) -- the real
    adapter must be tried first, but the virtual one still included as a fallback candidate, not
    dropped outright (asked for nothing to be silently excluded)."""
    import nmea2000processor.w2k2_download as w2k2_download

    payload = json.dumps(
        [
            {"Alias": "vEthernet (Default Switch)", "Description": "Hyper-V Virtual Ethernet Adapter",
             "IPv4": "172.23.192.1"},
            {"Alias": "Wi-Fi", "Description": "Intel(R) Wi-Fi 6E AX211 160MHz", "IPv4": "172.16.121.93"},
            {"Alias": "Bluetooth-netwerkverbinding", "Description": "Bluetooth Device (PAN)",
             "IPv4": "169.254.244.219"},
        ]
    )
    monkeypatch.setattr(
        w2k2_download.subprocess, "run", lambda *a, **kw: _FakeCompletedProcess(payload)
    )

    assert _windows_private_subnet_prefixes() == ["172.16.121.", "172.23.192."]


def test_windows_private_subnet_prefixes_handles_a_single_adapter_not_wrapped_in_a_list(monkeypatch):
    """ConvertTo-Json emits a bare object, not a one-element array, when there's only one match --
    a real PowerShell quirk, not something to special-case away only in a test fixture."""
    import nmea2000processor.w2k2_download as w2k2_download

    payload = json.dumps({"Alias": "Wi-Fi", "Description": "Intel Wi-Fi", "IPv4": "10.0.0.5"})
    monkeypatch.setattr(
        w2k2_download.subprocess, "run", lambda *a, **kw: _FakeCompletedProcess(payload)
    )

    assert _windows_private_subnet_prefixes() == ["10.0.0."]


def test_windows_private_subnet_prefixes_returns_empty_when_powershell_is_unavailable(monkeypatch):
    import nmea2000processor.w2k2_download as w2k2_download

    def _raise(*a, **kw):
        raise FileNotFoundError("powershell not found")

    monkeypatch.setattr(w2k2_download.subprocess, "run", _raise)

    assert _windows_private_subnet_prefixes() == []


def test_windows_private_subnet_prefixes_returns_empty_on_malformed_output(monkeypatch):
    import nmea2000processor.w2k2_download as w2k2_download

    monkeypatch.setattr(
        w2k2_download.subprocess, "run", lambda *a, **kw: _FakeCompletedProcess("not json")
    )

    assert _windows_private_subnet_prefixes() == []


def test_candidate_subnet_prefixes_falls_back_to_the_single_guess_when_enumeration_finds_nothing(
    monkeypatch,
):
    import nmea2000processor.w2k2_download as w2k2_download

    monkeypatch.setattr(w2k2_download, "_windows_private_subnet_prefixes", lambda: [])
    monkeypatch.setattr(w2k2_download, "_local_subnet_prefix", lambda: "10.169.127.")

    assert _candidate_subnet_prefixes() == ["10.169.127."]


class _ScriptedSocket:
    """Fakes socket.socket() for _local_subnet_prefix()'s own fallback chain: connect() either
    raises or succeeds depending on the target address, per a {address: local_ip_or_None} script
    (None means "raise OSError for this target")."""

    def __init__(self, script: dict):
        self._script = script
        self._connected_ip = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def setsockopt(self, *args):
        pass

    def connect(self, addr):
        ip = self._script.get(addr[0], "__unscripted__")
        if ip is None or ip == "__unscripted__":
            raise OSError("[WinError 10051] network is unreachable")
        self._connected_ip = ip

    def getsockname(self):
        return (self._connected_ip, 0)


def test_local_subnet_prefix_falls_back_to_broadcast_when_theres_no_route_to_the_internet(
    monkeypatch,
):
    """Regression test for a real bug, found in practice on a real Windows PC: connected only to
    the W2K-2's own isolated access point (no internet, no gateway at all) -- the primary
    "ask the OS which address it'd use to reach 8.8.8.8" trick needs a matching route to exist in
    the first place, and raises OSError (WinError 10051, "network unreachable") when there isn't
    one -- exactly the situation this needs to work in. Falls back to the same trick targeting the
    broadcast address instead, which needs no route table entry at all."""
    script = {"8.8.8.8": None, "255.255.255.255": "10.169.127.101"}
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: _ScriptedSocket(script))

    assert _local_subnet_prefix() == "10.169.127."


def test_local_subnet_prefix_falls_back_to_gethostbyname_ex_when_broadcast_also_fails(
    monkeypatch,
):
    script = {"8.8.8.8": None, "255.255.255.255": None}
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: _ScriptedSocket(script))
    monkeypatch.setattr(
        socket, "gethostbyname_ex", lambda name: ("host", [], ["127.0.0.1", "10.169.127.101"])
    )

    assert _local_subnet_prefix() == "10.169.127."


def test_local_subnet_prefix_reraises_when_every_fallback_finds_nothing_usable(monkeypatch):
    script = {"8.8.8.8": None, "255.255.255.255": None}
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: _ScriptedSocket(script))
    # Only loopback, nothing private -- the last fallback has nothing usable either.
    monkeypatch.setattr(socket, "gethostbyname_ex", lambda name: ("host", [], ["127.0.0.1"]))

    with pytest.raises(OSError):
        _local_subnet_prefix()


def test_discover_w2k2_returns_the_matching_host(monkeypatch):
    import nmea2000processor.w2k2_download as w2k2_download

    monkeypatch.setattr(w2k2_download, "_candidate_subnet_prefixes", lambda: ["10.0.0."])
    monkeypatch.setattr(w2k2_download, "_looks_like_w2k2", lambda ip: ip == "10.0.0.42")

    assert discover_w2k2() == "http://10.0.0.42"


def test_discover_w2k2_returns_none_when_nothing_on_any_subnet_matches(monkeypatch):
    import nmea2000processor.w2k2_download as w2k2_download

    monkeypatch.setattr(w2k2_download, "_candidate_subnet_prefixes", lambda: ["10.0.0."])
    monkeypatch.setattr(w2k2_download, "_looks_like_w2k2", lambda ip: False)

    assert discover_w2k2() is None


def test_discover_w2k2_tries_the_next_candidate_subnet_when_the_first_has_nothing(monkeypatch):
    """Regression test for a real bug found in practice, on a real dev machine: a Hyper-V virtual
    switch being up alongside the real wifi meant guessing just one "the" local subnet (see
    _local_subnet_prefix()) wasn't reliable -- discover_w2k2() must keep trying every candidate,
    not give up after the first subnet comes up empty."""
    import nmea2000processor.w2k2_download as w2k2_download

    monkeypatch.setattr(
        w2k2_download, "_candidate_subnet_prefixes", lambda: ["172.23.192.", "172.16.121."]
    )
    monkeypatch.setattr(w2k2_download, "_looks_like_w2k2", lambda ip: ip == "172.16.121.101")

    assert discover_w2k2() == "http://172.16.121.101"


def test_discover_w2k2_with_an_explicit_subnet_prefix_never_tries_others(monkeypatch):
    """An explicitly given subnet_prefix (used on Android, see this function's own doc comment)
    must be scanned alone -- never combined with _candidate_subnet_prefixes()'s own enumeration,
    which doesn't apply there at all (cellular's own subnet would just be noise)."""
    import nmea2000processor.w2k2_download as w2k2_download

    def _fail_if_called():
        raise AssertionError("_candidate_subnet_prefixes() must not be called with an explicit prefix")

    monkeypatch.setattr(w2k2_download, "_candidate_subnet_prefixes", _fail_if_called)
    monkeypatch.setattr(w2k2_download, "_looks_like_w2k2", lambda ip: ip == "10.0.0.42")

    assert discover_w2k2(subnet_prefix="10.0.0.") == "http://10.0.0.42"


def test_discover_w2k2_with_explicit_subnet_prefix_skips_self_detection(monkeypatch):
    """Android runs with both a hotspot and a cellular uplink active at once, so self-detecting
    the local subnet (via a UDP-connect to 8.8.8.8) would find the cellular subnet, not the
    hotspot's -- passing subnet_prefix explicitly must skip that self-detection entirely."""
    import nmea2000processor.w2k2_download as w2k2_download

    def _fail_if_called():
        raise AssertionError("_local_subnet_prefix() should not be called when subnet_prefix is given")

    monkeypatch.setattr(w2k2_download, "_local_subnet_prefix", _fail_if_called)
    monkeypatch.setattr(w2k2_download, "_looks_like_w2k2", lambda ip: ip == "192.168.43.7")

    assert discover_w2k2(subnet_prefix="192.168.43.") == "http://192.168.43.7"


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


def test_download_to_resumes_from_an_existing_partial_file(tmp_path, monkeypatch):
    """Regression test for a real bug found in practice: on a real, severely bandwidth-limited
    connection to the W2K-2 (well under 10 KB/s in one real case), every retry restarting from
    byte 0 meant a multi-megabyte file could never complete at all -- each attempt's own share of
    progress alone was smaller than the whole file. A partial file left by an earlier attempt must
    be resumed via an HTTP Range request, not thrown away."""
    import nmea2000processor.w2k2_download as w2k2_download

    target = tmp_path / "file.ebl"
    target.write_bytes(b"a" * 40)  # a previous attempt's own partial progress

    class _FakeResponse:
        status = 206  # Partial Content -- the server honored the Range request

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n):
            if not hasattr(self, "_served"):
                self._served = True
                return b"b" * 20
            return b""

    captured_request = {}

    def fake_urlopen(request, timeout=None):
        captured_request["headers"] = dict(request.header_items())
        return _FakeResponse()

    monkeypatch.setattr(w2k2_download.urllib.request, "urlopen", fake_urlopen)

    session = w2k2_download._Session("http://10.0.0.1")
    session.download_to("/api/download", {}, target, expected_size=60)

    assert captured_request["headers"].get("Range") == "bytes=40-"
    assert target.read_bytes() == b"a" * 40 + b"b" * 20  # appended, not overwritten


def test_download_to_restarts_from_scratch_when_the_server_ignores_the_range_request(
    tmp_path, monkeypatch
):
    """A server that doesn't support Range requests at all just returns the whole file again from
    the top (HTTP 200, not 206) -- must be detected and treated as a fresh download, not appended
    after the stale partial (which would silently corrupt the file)."""
    import nmea2000processor.w2k2_download as w2k2_download

    target = tmp_path / "file.ebl"
    target.write_bytes(b"a" * 40)

    class _FakeResponse:
        status = 200  # full content, Range header was ignored

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n):
            if not hasattr(self, "_served"):
                self._served = True
                return b"c" * 60  # the whole file, from the top
            return b""

    monkeypatch.setattr(w2k2_download.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse())

    session = w2k2_download._Session("http://10.0.0.1")
    session.download_to("/api/download", {}, target, expected_size=60)

    assert target.read_bytes() == b"c" * 60  # not b"a"*40 + b"c"*60 -- the stale partial is gone


def test_download_to_restarts_from_scratch_on_a_416_response(tmp_path, monkeypatch):
    """A partial that no longer aligns with what the server has (e.g. replaced/rotated between
    attempts, or a stale/corrupt local leftover) gets a 416 for its Range request -- discarded and
    retried fresh instead of repeating an identical, permanently-416ing request forever."""
    import nmea2000processor.w2k2_download as w2k2_download

    target = tmp_path / "file.ebl"
    target.write_bytes(b"a" * 40)

    calls = []

    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n):
            if not hasattr(self, "_served"):
                self._served = True
                return b"c" * 60
            return b""

    def fake_urlopen(request, timeout=None):
        calls.append(dict(request.header_items()))
        if len(calls) == 1:
            raise urllib.error.HTTPError(url="x", code=416, msg="Range Not Satisfiable", hdrs=None, fp=None)
        return _FakeResponse()

    monkeypatch.setattr(w2k2_download.urllib.request, "urlopen", fake_urlopen)

    session = w2k2_download._Session("http://10.0.0.1")
    session.download_to("/api/download", {}, target, expected_size=60)

    assert len(calls) == 2
    assert "Range" in calls[0]  # first attempt tried to resume
    assert "Range" not in calls[1]  # retry, after discarding the stale partial, starts fresh
    assert target.read_bytes() == b"c" * 60


def test_download_to_reports_bytes_received_so_far_when_it_times_out(tmp_path, monkeypatch):
    """Regression test: found in practice, a real 120s timeout on one real file gave no way to
    tell afterwards whether the connection was fully stalled (0 bytes) or genuinely slow but still
    making real progress -- needed to decide whether a shorter timeout (fail faster) or a longer
    one (or resuming instead of restarting from scratch) would actually help. The timeout message
    itself must say how far it actually got, not just that it gave up."""
    import nmea2000processor.w2k2_download as w2k2_download

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n):
            return b"x" * 1000  # keeps "succeeding" forever -- only the wall-clock check ends this

    # start, first wall-clock check (0s elapsed, proceeds to read one chunk), second check (past
    # _MAX_DOWNLOAD_SECONDS, raises) -- three calls total, matching download_to()'s own sequence.
    times = iter([0.0, 0.0, 1000.0])
    monkeypatch.setattr(w2k2_download.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(w2k2_download.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse())

    session = w2k2_download._Session("http://10.0.0.1")

    with pytest.raises(TimeoutError) as exc_info:
        session.download_to("/api/download", {}, tmp_path / "file.ebl", expected_size=5000)

    assert "1000 of 5000 bytes received" in str(exc_info.value)


def test_download_file_retries_and_succeeds_after_a_transient_connection_reset(tmp_path, monkeypatch):
    """A single transient connection reset (see the regression test above) must not give up on
    the file immediately -- retrying a couple of times is what actually recovers from a boat wifi
    hiccup instead of leaving that file (and every one queued after it this run) undownloaded."""
    import nmea2000processor.w2k2_download as w2k2_download

    calls = []

    class _FlakySession:
        def download_to(self, path, params, target, expected_size=None, should_cancel=None):
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


def test_download_file_skips_after_exhausting_retries_on_a_connection_error(tmp_path, monkeypatch):
    """Regression test for a real bug found in practice: this used to re-raise here, which aborted
    build_download_plan()'s entire remaining queue over one single file's own bad transfer (a
    real, otherwise-fine wifi link to the W2K-2 having one slow/dropped transfer among dozens of
    files is normal, not a sign every other file would fail too) -- now non-fatal, same as a
    persistent 404, left for _needs_download() to pick back up next run."""
    import nmea2000processor.w2k2_download as w2k2_download

    calls = []

    class _AlwaysFailsSession:
        def download_to(self, path, params, target, expected_size=None, should_cancel=None):
            calls.append(1)
            raise ConnectionResetError("connection reset")

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)

    download_file(
        _AlwaysFailsSession(), tmp_path, "EBL000001",
        {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
    )

    assert len(calls) == w2k2_download._DOWNLOAD_MAX_RETRIES + 1  # full retry budget used
    assert not (tmp_path / "EBL000001" / "000001_000.ebl").exists()


def test_download_file_skips_after_exhausting_retries_on_a_persistent_timeout(tmp_path, monkeypatch):
    """Same as the connection-error case above, but specifically for download_to()'s own wall-
    clock TimeoutError (see _MAX_DOWNLOAD_SECONDS) -- a plain OSError subclass, caught by the same
    branch, and exactly the real-world failure this regression was found from (a slow wifi link to
    the W2K-2 timing out on one file among many queued)."""
    import nmea2000processor.w2k2_download as w2k2_download

    class _AlwaysTimesOutSession:
        def download_to(self, path, params, target, expected_size=None, should_cancel=None):
            raise TimeoutError("no full file after 120s -- giving up on this attempt")

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)

    download_file(
        _AlwaysTimesOutSession(), tmp_path, "EBL000001",
        {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
    )

    assert not (tmp_path / "EBL000001" / "000001_000.ebl").exists()


def test_download_file_retries_a_404_and_succeeds_if_a_later_attempt_works(tmp_path, monkeypatch):
    """Regression test: a first version of this treated any 404 as "file permanently gone" and
    skipped immediately, no retry -- but found in practice, a 404 isn't always that: the W2K-2's
    simple embedded web server can return a spurious 404 under concurrent load (e.g. Android and
    the desktop CLI both hitting it at once) for a file that's still really there. A 404 must get
    the same retry budget as any other error."""
    import nmea2000processor.w2k2_download as w2k2_download

    calls = []

    class _FlakyServerSession:
        def download_to(self, path, params, target, expected_size=None, should_cancel=None):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(url="x", code=404, msg="Not Found", hdrs=None, fp=None)
            target.write_bytes(b"x" * 10)

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)

    download_file(
        _FlakyServerSession(), tmp_path, "EBL000001",
        {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
    )

    assert len(calls) == 2
    assert (tmp_path / "EBL000001" / "000001_000.ebl").read_bytes() == b"x" * 10


def test_download_file_skips_after_exhausting_retries_still_404(tmp_path, monkeypatch):
    """Only once a 404 survives the full retry budget is the file treated as genuinely gone --
    skipped (not fatal to the rest of the run), unlike other errors which still raise."""
    import nmea2000processor.w2k2_download as w2k2_download

    calls = []

    class _AlwaysGoneSession:
        def download_to(self, path, params, target, expected_size=None, should_cancel=None):
            calls.append(1)
            raise urllib.error.HTTPError(url="x", code=404, msg="Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)

    download_file(
        _AlwaysGoneSession(), tmp_path, "EBL000001",
        {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
    )

    assert len(calls) == w2k2_download._DOWNLOAD_MAX_RETRIES + 1  # full retry budget used
    assert not (tmp_path / "EBL000001" / "000001_000.ebl").exists()


def test_download_file_raises_after_exhausting_retries_on_a_non_404_http_error(tmp_path, monkeypatch):
    """Unlike a persistent 404 (skipped, not fatal), a persistent non-404 HTTP error still raises
    and aborts the run, same as before this change."""
    import nmea2000processor.w2k2_download as w2k2_download

    class _AlwaysFailsSession:
        def download_to(self, path, params, target, expected_size=None, should_cancel=None):
            raise urllib.error.HTTPError(url="x", code=503, msg="Service Unavailable", hdrs=None, fp=None)

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)

    with pytest.raises(urllib.error.HTTPError):
        download_file(
            _AlwaysFailsSession(), tmp_path, "EBL000001",
            {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
        )


def test_download_file_deletes_a_truncated_leftover_before_giving_up_on_repeated_404s(
    tmp_path, monkeypatch
):
    """Regression test for a real device sequence: attempt 1 writes a truncated file (wrong size,
    retried), then attempts 2 and 3 both 404 *inside* the request itself -- before ever reaching
    target.open("wb") again -- so the truncated file from attempt 1 was never touched again, and
    the final "[skip] ... still 404" message wrongly implied nothing local was left behind."""
    import nmea2000processor.w2k2_download as w2k2_download

    calls = []

    class _TruncatesThen404sSession:
        def download_to(self, path, params, target, expected_size=None, should_cancel=None):
            calls.append(1)
            if len(calls) == 1:
                target.write_bytes(b"x" * 3)  # truncated -- expected 10
            else:
                raise urllib.error.HTTPError(url="x", code=404, msg="Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)

    download_file(
        _TruncatesThen404sSession(), tmp_path, "EBL000001",
        {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
    )

    assert len(calls) == w2k2_download._DOWNLOAD_MAX_RETRIES + 1
    assert not (tmp_path / "EBL000001" / "000001_000.ebl").exists()


def test_download_file_deletes_a_truncated_leftover_before_skipping_on_repeated_connection_errors(
    tmp_path, monkeypatch
):
    """Same cleanup as the 404 case above, but for the connection-error path that now skips
    instead of raising -- a truncated leftover from an earlier attempt must not survive here
    either, or a size-only presence check would wrongly count it as already complete."""
    import nmea2000processor.w2k2_download as w2k2_download

    calls = []

    class _TruncatesThenFailsSession:
        def download_to(self, path, params, target, expected_size=None, should_cancel=None):
            calls.append(1)
            if len(calls) == 1:
                target.write_bytes(b"x" * 3)  # truncated -- expected 10
            else:
                raise ConnectionResetError("connection reset")

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)

    download_file(
        _TruncatesThenFailsSession(), tmp_path, "EBL000001",
        {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
    )

    assert not (tmp_path / "EBL000001" / "000001_000.ebl").exists()


def test_download_file_deletes_a_truncated_leftover_before_raising_on_a_non_404_error(
    tmp_path, monkeypatch
):
    """Same cleanup, but for the path that re-raises instead of skipping (a persistent non-404
    error) -- a truncated leftover from an earlier attempt must not survive here either."""
    import nmea2000processor.w2k2_download as w2k2_download

    calls = []

    class _TruncatesThenFailsSession:
        def download_to(self, path, params, target, expected_size=None, should_cancel=None):
            calls.append(1)
            if len(calls) == 1:
                target.write_bytes(b"x" * 3)  # truncated -- expected 10
            else:
                raise urllib.error.HTTPError(url="x", code=503, msg="Service Unavailable", hdrs=None, fp=None)

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)

    with pytest.raises(urllib.error.HTTPError):
        download_file(
            _TruncatesThenFailsSession(), tmp_path, "EBL000001",
            {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
        )

    assert not (tmp_path / "EBL000001" / "000001_000.ebl").exists()


def test_download_file_retries_a_truncated_transfer_and_succeeds_if_a_later_attempt_is_correct(
    tmp_path, monkeypatch
):
    """Regression test for a real device behavior: a transfer can complete with no error (a clean
    EOF, download_to() raises nothing) but still be short -- found in practice, a "successful"
    download that was silently a quarter of the file's real size, previously logged as a plain
    "[ok]" since nothing checked the actual result against info["file_size"]. Must be retried like
    any other failure, not accepted just because no exception was raised."""
    import nmea2000processor.w2k2_download as w2k2_download

    calls = []

    class _TruncatesFirstAttemptSession:
        def download_to(self, path, params, target, expected_size=None, should_cancel=None):
            calls.append(1)
            if len(calls) == 1:
                target.write_bytes(b"x" * 3)  # truncated -- expected 10
            else:
                target.write_bytes(b"x" * 10)

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)

    download_file(
        _TruncatesFirstAttemptSession(), tmp_path, "EBL000001",
        {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
    )

    assert len(calls) == 2
    assert (tmp_path / "EBL000001" / "000001_000.ebl").read_bytes() == b"x" * 10


def test_download_file_accepts_a_transfer_larger_than_the_stale_expected_size(tmp_path, monkeypatch):
    """Found in practice: a file the device is still actively writing to (not necessarily just the
    very last file overall -- see build_download_plan()) can grow between build_download_plan()'s
    folder listing and the actual download, so the transfer legitimately comes back with MORE
    bytes than info["file_size"] said to expect. An .ebl file only ever grows, never shrinks, so
    this is accepted on the first attempt rather than being endlessly retried and then deleted like
    a genuine (too few bytes) truncation would be."""
    import nmea2000processor.w2k2_download as w2k2_download

    calls = []

    class _GrewSinceListingSession:
        def download_to(self, path, params, target, expected_size=None, should_cancel=None):
            calls.append(1)
            target.write_bytes(b"x" * 12)  # more than the stale expected 10

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)

    download_file(
        _GrewSinceListingSession(), tmp_path, "EBL000001",
        {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
    )

    assert len(calls) == 1
    assert (tmp_path / "EBL000001" / "000001_000.ebl").read_bytes() == b"x" * 12


def test_download_file_deletes_a_still_truncated_file_after_exhausting_retries(tmp_path, monkeypatch):
    """If every attempt comes back short, the wrong-size file is deleted rather than left on disk
    -- otherwise it would be silently miscounted as "present" by anything that only checks
    existence (e.g. build_download_plan()'s already-complete-locally folder check), even though
    _needs_download()'s own size check would also have caught it on the next run regardless."""
    import nmea2000processor.w2k2_download as w2k2_download

    calls = []

    class _AlwaysTruncatesSession:
        def download_to(self, path, params, target, expected_size=None, should_cancel=None):
            calls.append(1)
            target.write_bytes(b"x" * 3)  # never matches the expected 10

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)

    download_file(
        _AlwaysTruncatesSession(), tmp_path, "EBL000001",
        {"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0},
    )

    assert len(calls) == w2k2_download._DOWNLOAD_MAX_RETRIES + 1
    assert not (tmp_path / "EBL000001" / "000001_000.ebl").exists()


def test_download_file_downloads_a_small_still_growing_file(tmp_path, monkeypatch):
    """The very last file (the one the W2K-2 may still be actively writing to) is downloaded like
    any other file, even while small -- a folder-closed-out assumption doesn't always hold in
    practice, and a file that's genuinely still short gets naturally retried next run anyway by
    download_file()'s own size check (asked for explicitly: an occasional wasted redownload beats
    silently sitting on stale/incomplete data)."""
    import nmea2000processor.w2k2_download as w2k2_download

    monkeypatch.setattr(w2k2_download.time, "sleep", lambda s: None)
    calls = []

    class _Session:
        def download_to(self, path, params, target, expected_size=None, should_cancel=None):
            calls.append(1)
            target.write_bytes(b"x" * 1000)

    download_file(
        _Session(), tmp_path, "EBL000001",
        {"file_name": "000001_005.ebl", "file_size": 1000, "file_time": 0},
    )

    assert calls == [1]
    assert (tmp_path / "EBL000001" / "000001_005.ebl").exists()


def test_will_download_true_for_a_missing_file(tmp_path: Path):
    assert _will_download(tmp_path / "nope.ebl", {"file_size": 1234}) is True


def test_will_download_false_for_an_already_complete_file(tmp_path: Path):
    target = tmp_path / "complete.ebl"
    target.write_bytes(b"1234")

    assert _will_download(target, {"file_size": 4}) is False


def test_build_download_plan_includes_every_file_regardless_of_size_or_position(tmp_path: Path, monkeypatch):
    """No file is ever excluded from the plan by size or by being the last file of the last
    folder -- every file the device reports gets downloaded (or skipped only because it's already
    complete locally, see _will_download())."""
    import nmea2000processor.w2k2_download as w2k2_download

    folders = [{"name": "EBL000001"}, {"name": "EBL000002"}]
    files = {
        "EBL000001": [{"file_name": "000001_000.ebl", "file_size": 10, "file_time": 0}],
        "EBL000002": [
            {"file_name": "000002_000.ebl", "file_size": 10, "file_time": 0},
            {"file_name": "000002_001.ebl", "file_size": 10, "file_time": 0},
        ],
    }
    monkeypatch.setattr(w2k2_download, "get_folders", lambda session: folders)
    monkeypatch.setattr(w2k2_download, "get_files", lambda session, folder: files[folder])

    plan, to_download = build_download_plan(session=object(), download_dir=tmp_path)

    assert {(folder, info["file_name"]) for folder, info in plan} == {
        ("EBL000001", "000001_000.ebl"),
        ("EBL000002", "000002_000.ebl"),
        ("EBL000002", "000002_001.ebl"),
    }
    assert sorted(info["file_name"] for info in to_download) == [
        "000001_000.ebl", "000002_000.ebl", "000002_001.ebl",
    ]


def test_build_download_plan_skips_a_non_last_folder_with_100_local_files(tmp_path: Path, monkeypatch):
    """A non-last folder is provably already closed out and always ends up
    with exactly _FILES_PER_FOLDER files -- once we already have that many locally, there's
    nothing left it could need, so its own file-list request (a real HTTP round trip) is skipped
    entirely (asked for explicitly)."""
    import nmea2000processor.w2k2_download as w2k2_download

    complete_dir = tmp_path / "EBL000001"
    complete_dir.mkdir()
    for i in range(w2k2_download._FILES_PER_FOLDER):
        (complete_dir / f"000001_{i:03d}.ebl").write_bytes(b"x")

    folders = [{"name": "EBL000001"}, {"name": "EBL000002"}]
    get_files_calls = []

    def fake_get_files(session, folder):
        get_files_calls.append(folder)
        return [{"file_name": "000002_000.ebl", "file_size": 10, "file_time": 0}]

    monkeypatch.setattr(w2k2_download, "get_folders", lambda session: folders)
    monkeypatch.setattr(w2k2_download, "get_files", fake_get_files)

    plan, to_download = build_download_plan(session=object(), download_dir=tmp_path)

    assert get_files_calls == ["EBL000002"]  # EBL000001 never listed at all
    assert {folder for folder, info in plan} == {"EBL000002"}


def test_build_download_plan_warns_about_a_partial_non_last_folder_but_still_checks_it(
    tmp_path: Path, monkeypatch
):
    """Fewer than _FILES_PER_FOLDER files locally for a non-last folder is unexpected (asked for
    explicitly) -- but unlike the complete case, we don't know which specific file(s) are missing
    without asking the device, so this folder is still listed and checked normally."""
    import nmea2000processor.w2k2_download as w2k2_download

    partial_dir = tmp_path / "EBL000001"
    partial_dir.mkdir()
    (partial_dir / "000001_000.ebl").write_bytes(b"x")  # only 1 of the expected 100

    folders = [{"name": "EBL000001"}, {"name": "EBL000002"}]
    get_files_calls = []

    def fake_get_files(session, folder):
        get_files_calls.append(folder)
        return [{"file_name": f"{folder.lower()}_000.ebl", "file_size": 10, "file_time": 0}]

    logged = []
    monkeypatch.setattr(w2k2_download, "get_folders", lambda session: folders)
    monkeypatch.setattr(w2k2_download, "get_files", fake_get_files)
    monkeypatch.setattr(w2k2_download, "log", lambda message, **kwargs: logged.append(message))

    build_download_plan(session=object(), download_dir=tmp_path)

    assert get_files_calls == ["EBL000001", "EBL000002"]
    assert any("only 1 of the expected 100" in message for message in logged)


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
