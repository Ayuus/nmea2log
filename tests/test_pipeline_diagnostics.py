"""What gets logged when something unexpected goes wrong while decoding one file -- see
pipeline._FileProcessing."""

from datetime import datetime
from pathlib import Path

import pytest

from nmea2log.log import log_exception
from nmea2log.model import PositionFix, SogSample
from nmea2log.pipeline import PipelineCancelled, PipelineError, _FileProcessing, _describe_samples


def _samples(sogs):
    fixes = {10: [PositionFix(datetime(2026, 7, 15, 9, 0), 52.3, 4.9)]}
    return (fixes, {10: sogs}) + ({},) * 11


def test_describe_samples_shows_the_count_and_types_per_part_and_source():
    sogs = [SogSample(datetime(2026, 7, 15, 9, 0, i), 3.0) for i in range(2)]

    description = _describe_samples(_samples(sogs))

    assert "fixes={10: 1xPositionFix}" in description
    assert "sogs={10: 2xSogSample}" in description


def test_describe_samples_shows_a_list_holding_two_types():
    sogs = [SogSample(datetime(2026, 7, 15, 9, 0), 3.0), PositionFix(datetime(2026, 7, 15, 9, 0), 52.3, 4.9)]

    assert "sogs={10: 2xPositionFix/SogSample}" in _describe_samples(_samples(sogs))


def test_an_unexpected_error_in_a_file_logs_where_it_was_and_still_propagates(log_lines):
    path = Path("Actisense/EBL000011/000011_034.ebl")
    samples = _samples([SogSample(datetime(2026, 7, 15, 9, 0), 3.0)])

    with pytest.raises(AttributeError):
        with _FileProcessing(path, 1134, 2326) as ctx:
            ctx.step = "merging the samples into the season arrays"
            ctx.samples = samples
            raise AttributeError("'PositionFix' object has no attribute 'sog_ms'")

    text = "\n".join(log_lines) if log_lines else ""
    # log_exception writes to stderr by default; the sink only sees what passes the console level.
    assert "file 1134/2326" in text
    assert "000011_034.ebl" in text
    assert "while merging the samples into the season arrays" in text
    assert "decoded fresh" in text
    assert "Traceback (most recent call last)" in text
    assert "no attribute 'sog_ms'" in text
    assert "Its samples: fixes={10: 1xPositionFix}; sogs={10: 1xSogSample}" in text


@pytest.mark.parametrize("exc", [PipelineCancelled(), PipelineError("Log file not found")])
def test_expected_pipeline_exceptions_are_not_logged_as_unexpected_errors(log_lines, exc):
    with pytest.raises(type(exc)):
        with _FileProcessing(Path("a.ebl"), 1, 1):
            raise exc

    assert log_lines == []


def test_log_exception_logs_the_context_and_every_traceback_line(log_lines):
    try:
        raise ValueError("boom")
    except ValueError:
        log_exception("While testing")

    assert any("[error] While testing: ValueError('boom')" in line for line in log_lines)
    assert any("[error]   Traceback" in line for line in log_lines)
    assert any("ValueError: boom" in line for line in log_lines)
