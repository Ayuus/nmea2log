import pytest


@pytest.fixture(autouse=True)
def _isolate_cwd_from_the_real_nmea2log_ini(tmp_path, monkeypatch):
    """Every test runs with its cwd pointed at an empty tmp directory, never this project's own
    directory. ``config.DEFAULT_CONFIG_PATH`` resolves "nmea2log.ini" relative to the current
    working directory, so without this, any test that builds an argparse namespace (via
    ``build_arg_parser()``/``main()``) picks up this project's own real ``nmea2log.ini`` as
    config defaults -- including its real, complete ``[upload]`` settings and real SFTP credentials.

    Found in practice: a test with a fixed, deterministic single-trip fixture (see
    ``_run_with_one_trip`` in test_cli.py) that didn't isolate its cwd silently uploaded that
    fixture to the live production site every time it ran, overwriting the real 15-trip logbook
    -- with no trace in ``nmea2log.log`` either, since that log path is also cwd-relative and so
    went to the tmp test directory instead. Looked exactly like a mysteriously reappearing
    "empty" trips list with no apparent cause; took a while to trace back to "every time the test
    suite runs" being the actual trigger.

    A test that specifically wants to exercise config-file loading from a real-looking directory
    still works fine: it just passes its own path explicitly (e.g. ``load_config(config_path)``,
    ``main(["--config", str(config_path), ...])``) rather than relying on an implicit cwd
    lookup."""
    monkeypatch.chdir(tmp_path)
