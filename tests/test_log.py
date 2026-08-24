import io
from datetime import datetime, timedelta

from nmea2000processor import log as log_module
from nmea2000processor.log import log, set_log_file


def test_log_prints_a_timestamped_line_to_the_given_stream():
    stream = io.StringIO()

    log("hello", file=stream)

    output = stream.getvalue()
    assert output.rstrip("\n").endswith("hello")
    assert output[:4].isdigit()  # starts with a year, e.g. "2026-..."


def test_log_stamps_every_line_of_a_multi_line_message():
    """Regression test for a real bug: a lot of what gets logged is a multi-line dump of someone
    else's output (e.g. the sftp client's own error text, banner included), and with only the
    first line stamped, skimming the log to see when a several-line block happened meant hunting
    upward for the nearest timestamp above it (found in practice, asked for explicitly)."""
    stream = io.StringIO()

    log("first line\nsecond line\nthird line", file=stream)

    lines = stream.getvalue().rstrip("\n").split("\n")
    assert len(lines) == 3
    for rendered_line in lines:
        assert rendered_line[:4].isdigit()  # starts with a year, e.g. "2026-..."
    assert lines[0].endswith("first line")
    assert lines[1].endswith("second line")
    assert lines[2].endswith("third line")


def test_set_log_file_also_writes_every_subsequent_log_call_to_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(log_module, "_log_file", None)  # isolate from other tests' leftover state
    log_path = tmp_path / "nmea2log.log"
    stream = io.StringIO()

    set_log_file(log_path)
    try:
        log("first line", file=stream)
        log("second line", file=stream)
    finally:
        log_module._log_file.close()

    contents = log_path.read_text(encoding="utf-8")
    assert "first line" in contents
    assert "second line" in contents
    # still goes to the given stream too, not only the file
    assert "first line" in stream.getvalue()


def test_set_log_file_appends_across_runs_instead_of_overwriting(tmp_path, monkeypatch):
    """A log file that gets overwritten on every run would only ever show the most recent one --
    the whole point here is a history to check back on later (e.g. how a --backup-ebl run that
    got interrupted progressed across several separate runs)."""
    monkeypatch.setattr(log_module, "_log_file", None)
    log_path = tmp_path / "nmea2log.log"
    log_path.write_text("previous run\n", encoding="utf-8")

    set_log_file(log_path)
    try:
        log("this run", file=io.StringIO())
    finally:
        log_module._log_file.close()

    contents = log_path.read_text(encoding="utf-8")
    assert "previous run" in contents
    assert "this run" in contents


def test_set_log_file_prunes_lines_older_than_retention_days(tmp_path, monkeypatch):
    """Regression test: appending forever (see the test above) with nothing ever trimming the
    file meant it grew without bound (found in practice, asked for explicitly)."""
    monkeypatch.setattr(log_module, "_log_file", None)
    log_path = tmp_path / "nmea2log.log"
    now = datetime.now()
    old_line = (now - timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S") + " too old, should be dropped\n"
    recent_line = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S") + " recent, should stay\n"
    log_path.write_text(old_line + recent_line, encoding="utf-8")

    set_log_file(log_path, retention_days=90)
    log_module._log_file.close()

    contents = log_path.read_text(encoding="utf-8")
    assert "too old" not in contents
    assert "recent, should stay" in contents


def test_set_log_file_keeps_unparseable_lines_when_pruning(tmp_path, monkeypatch):
    """A line that doesn't start with a timestamp (e.g. hand-edited, or from before this file even
    had timestamps) is kept rather than risk silently losing something unexpected."""
    monkeypatch.setattr(log_module, "_log_file", None)
    log_path = tmp_path / "nmea2log.log"
    log_path.write_text("not a timestamped line\n", encoding="utf-8")

    set_log_file(log_path, retention_days=1)
    log_module._log_file.close()

    assert "not a timestamped line" in log_path.read_text(encoding="utf-8")
