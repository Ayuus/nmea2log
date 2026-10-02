import os

import pytest

from nmea2log.fileutil import write_bytes_atomic


def test_writes_the_payload_and_leaves_no_temporary_file(tmp_path):
    target = tmp_path / "entry.pkl.zz"

    write_bytes_atomic(target, b"payload")

    assert target.read_bytes() == b"payload"
    assert [p.name for p in tmp_path.iterdir()] == ["entry.pkl.zz"]


def test_replaces_an_existing_file(tmp_path):
    target = tmp_path / "entry.pkl.zz"
    target.write_bytes(b"old")

    write_bytes_atomic(target, b"new")

    assert target.read_bytes() == b"new"


def test_a_failed_move_keeps_the_old_content_and_cleans_up(tmp_path, monkeypatch):
    target = tmp_path / "entry.pkl.zz"
    target.write_bytes(b"old")

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError):
        write_bytes_atomic(target, b"new")

    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["entry.pkl.zz"]
