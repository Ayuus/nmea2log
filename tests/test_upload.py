import subprocess
from pathlib import Path

import pytest

from nmea2000processor.upload import (
    UploadError,
    _local_to_sftp_path,
    list_remote_filenames,
    upload_file,
    upload_files,
)


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
        local, host="example.com", user="me", remote_path="private/little_endian/logbook.html",
        key_file=key_file,
    )

    lines = captured["batch_contents"].splitlines()
    assert lines[0] == (
        f'put "{_local_to_sftp_path(local)}" "private/little_endian/logbook.html.tmp-upload"'
    )
    assert lines[1] == (
        'rename "private/little_endian/logbook.html.tmp-upload" "private/little_endian/logbook.html"'
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


def test_list_remote_filenames_parses_bare_names(tmp_path: Path, monkeypatch):
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")
    captured = {}

    def fake_run(cmd, capture_output, text):
        batch_path = Path(cmd[cmd.index("-b") + 1])
        captured["batch_contents"] = batch_path.read_text(encoding="utf-8")
        return _FakeCompletedProcess(returncode=0, stdout="a.ebl\nb.ebl\n\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    names = list_remote_filenames(
        host="example.com", user="me", remote_dir="private/ebl-backup", key_file=key_file,
    )

    assert names == {"a.ebl", "b.ebl"}
    # the directory is created first (error ignored via "-") so "ls" always has something to
    # list, instead of having to tell "doesn't exist yet" apart from a real failure by matching
    # on the sftp client's own (inconsistently worded) error text
    assert '-mkdir "private/ebl-backup"' in captured["batch_contents"]
    assert 'ls -1 "private/ebl-backup"' in captured["batch_contents"]


def test_list_remote_filenames_returns_empty_set_for_a_freshly_created_directory(tmp_path: Path, monkeypatch):
    """A first-ever backup run: the remote directory doesn't exist yet, so it gets created (empty)
    right before the "ls" -- which then succeeds with no output, not an empty set as an error
    workaround."""
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")

    def fake_run(cmd, capture_output, text):
        return _FakeCompletedProcess(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    names = list_remote_filenames(
        host="example.com", user="me", remote_dir="private/ebl-backup", key_file=key_file,
    )

    assert names == set()


def test_list_remote_filenames_raises_on_other_failures(tmp_path: Path, monkeypatch):
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")

    def fake_run(cmd, capture_output, text):
        return _FakeCompletedProcess(returncode=1, stderr="Permission denied (publickey).")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("nmea2000processor.upload.time.sleep", lambda s: None)

    with pytest.raises(UploadError, match="Permission denied"):
        list_remote_filenames(host="example.com", user="me", remote_dir="private/ebl-backup", key_file=key_file)


def test_list_remote_filenames_retries_and_succeeds_after_a_transient_failure(tmp_path: Path, monkeypatch, capsys):
    """Regression test for a real bug: the SFTP connection to a shared-hosting server reset out of
    the blue, even on the very first command of a brand new session -- retrying the whole batch a
    couple of times before giving up is what actually recovers from it (found in practice: the
    exact same batch, replayed by hand right after a failure, succeeded instantly)."""
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")
    calls = []

    def fake_run(cmd, capture_output, text):
        calls.append(1)
        if len(calls) == 1:
            return _FakeCompletedProcess(returncode=1, stderr="Connection reset by peer")
        return _FakeCompletedProcess(returncode=0, stdout="a.ebl\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("nmea2000processor.upload.time.sleep", lambda s: None)

    names = list_remote_filenames(host="example.com", user="me", remote_dir="private/ebl-backup", key_file=key_file)

    assert names == {"a.ebl"}
    assert len(calls) == 2
    err = capsys.readouterr().err
    assert "attempt 1/3, retrying" in err
    # the message itself starts on its own line, not appended right after "failed (attempt...):"
    assert "):\n" in err or "):\r\n" in err


def test_upload_files_is_a_noop_for_an_empty_list(tmp_path: Path, monkeypatch):
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: calls.append(1))

    upload_files([], host="example.com", user="me", remote_dir="private/ebl-backup", key_file=key_file)

    assert calls == []


def test_upload_files_uploads_every_file_in_one_session(tmp_path: Path, monkeypatch):
    a = tmp_path / "a.ebl"
    b = tmp_path / "b.ebl"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")

    captured = {}

    def fake_run(cmd, capture_output, text):
        captured["cmd"] = cmd
        batch_path = Path(cmd[cmd.index("-b") + 1])
        captured["batch_contents"] = batch_path.read_text(encoding="utf-8")
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    upload_files([a, b], host="example.com", user="me", remote_dir="private/ebl-backup", key_file=key_file)

    # one sftp connection for both files, not one per file
    assert captured["cmd"].count("sftp") == 1
    batch = captured["batch_contents"]
    assert '-mkdir "private/ebl-backup"' in batch
    assert f'put "{_local_to_sftp_path(a)}" "private/ebl-backup/a.ebl"' in batch
    assert f'put "{_local_to_sftp_path(b)}" "private/ebl-backup/b.ebl"' in batch


def test_upload_files_uses_forward_slashes_for_a_windows_local_path(tmp_path: Path, monkeypatch):
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")
    captured = {}

    def fake_run(cmd, capture_output, text):
        batch_path = Path(cmd[cmd.index("-b") + 1])
        captured["batch_contents"] = batch_path.read_text(encoding="utf-8")
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    windows_path = Path(r"C:\Users\skipper\Actisense\2026-08-18-1200.ebl")

    upload_files([windows_path], host="example.com", user="me", remote_dir="private/ebl-backup", key_file=key_file)

    assert "\\" not in captured["batch_contents"]
    assert "C:/Users/skipper/Actisense/2026-08-18-1200.ebl" in captured["batch_contents"]


def test_upload_files_raises_with_the_sftp_error_message(tmp_path: Path, monkeypatch):
    a = tmp_path / "a.ebl"
    a.write_text("a", encoding="utf-8")
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")

    def fake_run(cmd, capture_output, text):
        return _FakeCompletedProcess(returncode=1, stderr="Connection timed out")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("nmea2000processor.upload.time.sleep", lambda s: None)

    with pytest.raises(UploadError, match="Connection timed out"):
        upload_files([a], host="example.com", user="me", remote_dir="private/ebl-backup", key_file=key_file)


def test_upload_files_retries_and_succeeds_after_a_transient_failure(tmp_path: Path, monkeypatch):
    a = tmp_path / "a.ebl"
    a.write_text("a", encoding="utf-8")
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake key", encoding="utf-8")
    calls = []

    def fake_run(cmd, capture_output, text):
        calls.append(1)
        if len(calls) == 1:
            return _FakeCompletedProcess(returncode=1, stderr="Connection reset by peer")
        return _FakeCompletedProcess(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("nmea2000processor.upload.time.sleep", lambda s: None)

    upload_files([a], host="example.com", user="me", remote_dir="private/ebl-backup", key_file=key_file)

    assert len(calls) == 2
