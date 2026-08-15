import subprocess
from pathlib import Path

import pytest

from nmea2000processor.upload import UploadError, upload_file


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_upload_file_raises_if_key_file_is_missing(tmp_path: Path):
    local = tmp_path / "logbook.html"
    local.write_text("hi", encoding="utf-8")

    with pytest.raises(UploadError, match="SSH key not found"):
        upload_file(
            local, host="example.com", user="me", remote_path="logbook.html",
            key_file=tmp_path / "does_not_exist",
        )


def test_upload_file_calls_sftp_in_batch_mode_with_the_key(tmp_path: Path, monkeypatch):
    local = tmp_path / "logbook.html"
    local.write_text("hi", encoding="utf-8")
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")

    captured = {}

    def fake_run(cmd, capture_output, text):
        captured["cmd"] = cmd
        # find the batch file passed after -b and check its contents while it still exists
        batch_path = Path(cmd[cmd.index("-b") + 1])
        captured["batch_contents"] = batch_path.read_text(encoding="utf-8")
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    upload_file(
        local, host="example.com", user="me", remote_path="site/logboek/logbook.html",
        key_file=key_file, port=2222,
    )

    cmd = captured["cmd"]
    assert cmd[0] == "sftp"
    assert "-i" in cmd and str(key_file) in cmd
    assert "-P" in cmd and "2222" in cmd
    assert "BatchMode=yes" in cmd
    assert "StrictHostKeyChecking=accept-new" in cmd
    assert cmd[-1] == "me@example.com"
    assert str(local).replace("\\", "/") in captured["batch_contents"]
    assert "site/logboek/logbook.html" in captured["batch_contents"]


def test_upload_file_uses_forward_slashes_for_a_windows_local_path(tmp_path: Path, monkeypatch):
    """Regression test for a real bug: sftp's own batch-file parser treats backslash as an
    escape character in "put" arguments, so a Windows path's backslashes silently vanished
    instead of being treated as path separators (e.g. "C:\\Users\\...\\logbook.html" turned into
    "C:UsersLogbook.html"), which then failed with "No such file or directory" (found in
    practice)."""
    local = tmp_path / "logbook.html"
    local.write_text("hi", encoding="utf-8")
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")

    captured = {}

    def fake_run(cmd, capture_output, text):
        batch_path = Path(cmd[cmd.index("-b") + 1])
        captured["batch_contents"] = batch_path.read_text(encoding="utf-8")
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    windows_path = Path(r"C:\Users\skipper\AppData\Local\Temp\logbook.html")

    upload_file(windows_path, host="example.com", user="me", remote_path="logbook.html", key_file=key_file)

    assert "\\" not in captured["batch_contents"].split(" ")[1]
    assert "C:/Users/skipper/AppData/Local/Temp/logbook.html" in captured["batch_contents"]


def test_upload_file_deletes_the_batch_file_afterwards(tmp_path: Path, monkeypatch):
    local = tmp_path / "logbook.html"
    local.write_text("hi", encoding="utf-8")
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")

    seen_batch_path = {}

    def fake_run(cmd, capture_output, text):
        seen_batch_path["path"] = Path(cmd[cmd.index("-b") + 1])
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    upload_file(local, host="example.com", user="me", remote_path="logbook.html", key_file=key_file)

    assert not seen_batch_path["path"].exists()


def test_upload_file_raises_with_the_sftp_error_message(tmp_path: Path, monkeypatch):
    local = tmp_path / "logbook.html"
    local.write_text("hi", encoding="utf-8")
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")

    def fake_run(cmd, capture_output, text):
        return _FakeCompletedProcess(returncode=1, stderr="Permission denied (publickey).")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(UploadError, match="Permission denied"):
        upload_file(local, host="example.com", user="me", remote_path="logbook.html", key_file=key_file)
