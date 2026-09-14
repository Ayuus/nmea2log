import base64
import io
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from nmea2000processor.upload import UploadError, _local_to_sftp_path, upload_file, upload_via_rest


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


def test_upload_file_uploads_to_a_temp_name_then_renames_into_place(tmp_path: Path, monkeypatch):
    """Regression test for a real bug: uploading straight to remote_path overwrites it in place,
    which isn't atomic -- a page reading that file (e.g. the WordPress gatekeeper for the HTML
    logbook, which reads it fresh on every request) can be served a truncated file if it's read
    mid-upload. Uploading to a temp name and renaming it into place at the end (POSIX rename() is
    atomic) means a concurrent read always gets either the complete old file or the complete new
    one, never a partial one."""
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

    upload_file(
        local, host="example.com", user="me", remote_path="private/logboek/logbook.html",
        key_file=key_file,
    )

    lines = captured["batch_contents"].splitlines()
    assert lines[0] == (
        f'put "{_local_to_sftp_path(local)}" "private/logboek/logbook.html.tmp-upload"'
    )
    assert lines[1] == (
        'rename "private/logboek/logbook.html.tmp-upload" "private/logboek/logbook.html"'
    )


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


def test_upload_file_strips_the_servers_login_banner_from_the_error_message(tmp_path: Path, monkeypatch):
    """Regression test for a real bug: TransIP's SFTP server shows a multi-line legal disclaimer
    ("Unauthorized access to this system/network is prohibited.") on every single connection,
    successful or not -- buried the actual error (Connection reset by peer) under several lines
    of unrelated boilerplate, making it look like something was wrong with the connection itself
    rather than a transient reset (found in practice)."""
    local = tmp_path / "logbook.html"
    local.write_text("hi", encoding="utf-8")
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")
    banner = (
        "** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
        "**************************************************************\n"
        "*                                                            *\n"
        "*               Unauthorized access to this                  *\n"
        "*               system/network is prohibited.                *\n"
        "*                                                            *\n"
        "**************************************************************\n"
    )

    def fake_run(cmd, capture_output, text):
        return _FakeCompletedProcess(returncode=1, stderr=banner + "Connection reset by peer")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(UploadError) as excinfo:
        upload_file(local, host="example.com", user="me", remote_path="logbook.html", key_file=key_file)

    assert str(excinfo.value) == "Connection reset by peer"


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


class _FakeHttpResponse:
    def __init__(self, body: bytes = b'{"ok":true}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def test_upload_via_rest_sends_basic_auth_and_the_raw_body(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = dict(request.header_items())
        captured["data"] = request.data
        return _FakeHttpResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    upload_via_rest(
        b"<html>logbook</html>",
        url="https://example.org/wp-json/nmea2log/v1/logbook",
        user="alice",
        app_password="abcd efgh ijkl mnop",
    )

    assert captured["url"] == "https://example.org/wp-json/nmea2log/v1/logbook"
    assert captured["method"] == "POST"
    assert captured["data"] == b"<html>logbook</html>"
    expected = base64.b64encode(b"alice:abcd efgh ijkl mnop").decode("ascii")
    assert captured["headers"]["Authorization"] == f"Basic {expected}"
    assert captured["headers"]["Content-type"] == "text/html; charset=utf-8"


def test_upload_via_rest_raises_with_the_servers_own_error_message(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            url=request.full_url, code=400, msg="Bad Request", hdrs=None,
            fp=io.BytesIO(b'{"code":"logbook_too_small","message":"Uploaded content looks incomplete"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(UploadError, match="logbook_too_small"):
        upload_via_rest(b"x", url="https://example.org/wp-json/nmea2log/v1/logbook", user="alice", app_password="pw")


def test_upload_via_rest_raises_on_a_network_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(UploadError, match="Connection refused"):
        upload_via_rest(b"x", url="https://example.org/wp-json/nmea2log/v1/logbook", user="alice", app_password="pw")
