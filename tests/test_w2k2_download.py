import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from nmea2000processor.w2k2_download import _needs_download, download_files, load_config, main


def test_load_config_from_ini(tmp_path: Path):
    config_path = tmp_path / "w2k2.ini"
    config_path.write_text(
        "[w2k2]\n"
        "url = http://192.168.1.50\n"
        "user = skipper\n"
        "password = geheim\n"
        "download_dir = mijn_logs\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.url == "http://192.168.1.50"
    assert config.user == "skipper"
    assert config.password == "geheim"
    assert config.download_dir == Path("mijn_logs")
    assert config.token is None


def test_load_config_missing_file_uses_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("W2K2_URL", raising=False)
    monkeypatch.delenv("W2K2_TOKEN", raising=False)
    monkeypatch.delenv("W2K2_USER", raising=False)
    monkeypatch.delenv("W2K2_PASS", raising=False)
    monkeypatch.delenv("W2K2_DOWNLOAD_DIR", raising=False)

    config = load_config(tmp_path / "does_not_exist.ini")

    assert config.url == "http://10.164.231.101"
    assert config.download_dir == Path("Actisense")
    assert config.token is None
    assert config.user is None
    assert config.password is None


def test_load_config_env_vars_override_file(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "w2k2.ini"
    config_path.write_text("[w2k2]\nurl = http://192.168.1.50\nuser = from-file\n", encoding="utf-8")
    monkeypatch.setenv("W2K2_USER", "from-env-var")

    config = load_config(config_path)

    assert config.user == "from-env-var"
    assert config.url == "http://192.168.1.50"  # not overridden, stays from the file


def test_main_reports_a_clear_error_on_timeout_instead_of_a_raw_traceback(monkeypatch, tmp_path):
    """Regression test for a real crash: a read timeout while logging in (e.g. the boat's wifi
    isn't reachable) surfaced as a raw Python traceback instead of the same clean "[error]
    network: ..." message a connection failure already got -- TimeoutError isn't a subclass of
    urllib.error.URLError, so it slipped past the except clause."""
    config_path = tmp_path / "w2k2.ini"
    config_path.write_text(
        "[w2k2]\nurl = http://10.164.231.101\nuser = skipper\npassword = geheim\n",
        encoding="utf-8",
    )

    def fake_urlopen(request, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(config_path)])

    assert "network" in str(exc_info.value)


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


class _FakeSession:
    """Stands in for a real W2K-2 connection: download_to() just sleeps (simulating a slow
    per-file transfer) and writes a fixed payload, so tests can measure whether calls actually
    overlapped instead of hitting a real device."""

    def __init__(self, delay: float = 0.2, fail_on: str = None):
        self.delay = delay
        self.fail_on = fail_on

    def download_to(self, path, params, target):
        if self.fail_on and self.fail_on in params["file_name"]:
            raise urllib.error.HTTPError(params["file_name"], 401, "unauthorized", None, None)
        time.sleep(self.delay)
        target.write_bytes(b"x" * 10)


def _fake_files(count: int):
    return [{"file_name": f"{i}.ebl", "file_size": 10, "file_time": 0} for i in range(count)]


def test_download_files_runs_concurrently(tmp_path: Path):
    """Regression guard for the whole point of max_workers: 4 files that each take 0.2s must
    finish in well under the 0.8s a sequential run would take, proving the downloads actually
    overlap instead of max_workers being a no-op."""
    session = _FakeSession(delay=0.2)
    files = _fake_files(4)

    start = time.monotonic()
    download_files(session, tmp_path, "EBL000001", files, max_workers=4)
    elapsed = time.monotonic() - start

    assert elapsed < 0.6
    for i in range(4):
        assert (tmp_path / "EBL000001" / f"{i}.ebl").exists()


def test_download_files_sequential_when_max_workers_is_one(tmp_path: Path):
    session = _FakeSession(delay=0.05)
    files = _fake_files(3)

    download_files(session, tmp_path, "EBL000001", files, max_workers=1)

    for i in range(3):
        assert (tmp_path / "EBL000001" / f"{i}.ebl").exists()


def test_download_files_propagates_a_failed_download(tmp_path: Path):
    """A failure in one concurrent download (e.g. an expired token) must still surface to the
    caller instead of being silently swallowed by the thread pool."""
    session = _FakeSession(delay=0.01, fail_on="2.ebl")
    files = _fake_files(4)

    with pytest.raises(urllib.error.HTTPError):
        download_files(session, tmp_path, "EBL000001", files, max_workers=4)
