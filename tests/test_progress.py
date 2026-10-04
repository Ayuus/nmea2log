from nmea2log.progress import BUILDING_TRIPS, DECODING, parse_progress_line, sink_with_progress


def test_a_decode_line_gives_current_and_total():
    assert parse_progress_line("2026-10-04 15:39:27 [info] ...decoded 534/2326 logfile(s) so far") == (DECODING, 534, 2326)


def test_the_four_build_checkpoints_are_steps_one_to_four():
    lines = [
        "[info] Building trips from 2711856 GPS position(s)...",
        "[info] ...322056 navigation samples merged, classifying trips...",
        "[info] ...111 run(s) classified, computing per-trip statistics...",
        "[info] 25 trip(s) found, writing logbook...",
    ]

    assert [parse_progress_line(line) for line in lines] == [(BUILDING_TRIPS, step, 4) for step in (1, 2, 3, 4)]


def test_other_lines_are_not_progress():
    assert parse_progress_line("[info] Log files: 2326 .ebl file(s) in 24 folder(s)") is None


def test_the_log_texts_the_pipeline_really_logs_are_recognised():
    """The regexes must match the texts of the log() calls themselves, not only these examples."""
    import inspect

    from nmea2log import android_entry, cli, pipeline, tripbuilder

    source = "".join(inspect.getsource(module) for module in (android_entry, cli, pipeline, tripbuilder))
    for fragment in ("logfile(s) so far", "navigation samples merged, classifying trips", "classified, computing per-trip statistics", "found, writing logbook", "Building trips from"):
        assert fragment in source, fragment


class _Callback:
    def __init__(self, with_progress):
        self.lines = []
        self.progress = []
        if with_progress:
            self.onProgress = lambda phase, current, total: self.progress.append((phase, current, total))

    def onLogLine(self, line):
        self.lines.append(line)


def test_the_sink_passes_every_line_and_reports_progress_lines():
    callback = _Callback(with_progress=True)
    sink = sink_with_progress(callback)

    sink("[info] hello")
    sink("[info] ...decoded 1/2 logfile(s) so far")

    assert callback.lines == ["[info] hello", "[info] ...decoded 1/2 logfile(s) so far"]
    assert callback.progress == [(DECODING, 1, 2)]


def test_a_callback_without_on_progress_still_gets_its_lines():
    callback = _Callback(with_progress=False)

    sink_with_progress(callback)("[info] ...decoded 1/2 logfile(s) so far")

    assert callback.lines == ["[info] ...decoded 1/2 logfile(s) so far"]
