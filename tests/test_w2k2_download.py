import urllib.request
from pathlib import Path

import pytest

from nmea2000processor.w2k2_download import _needs_download, load_config, main


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
