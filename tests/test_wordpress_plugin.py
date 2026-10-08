import hashlib
import importlib.util
import io
import json
import re
import zipfile
from pathlib import Path

PLUGIN_FOLDER = Path(__file__).resolve().parents[1] / "wordpress-plugin"


def _builder():
    spec = importlib.util.spec_from_file_location("build_release", PLUGIN_FOLDER / "build_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_header_says_where_updates_come_from():
    source = (PLUGIN_FOLDER / "nmea2log-remarks.php").read_text(encoding="utf-8")

    assert re.search(r"^\s*\*\s*Update URI:\s*https://github\.com/Ayuus/nmea2log\s*$", source, re.MULTILINE)
    assert "define('NMEA2LOG_UPDATE_URI', 'https://github.com/Ayuus/nmea2log');" in source


def test_update_files_are_what_the_plugin_as_it_is_now_makes():
    # Fails after a change to nmea2log-remarks.php until `python wordpress-plugin/build_release.py` has been run (and the
    # Version in the header raised): the plugin on a site would otherwise be offered an update that is not this file.
    package, manifest = _builder().build()

    assert json.loads((PLUGIN_FOLDER / "update.json").read_text(encoding="utf-8")) == manifest
    assert (PLUGIN_FOLDER / "nmea2log-remarks.zip").read_bytes() == package


def test_zip_holds_the_plugin_in_a_folder_of_its_own():
    package = (PLUGIN_FOLDER / "nmea2log-remarks.zip").read_bytes()

    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        assert archive.namelist() == ["nmea2log-remarks/nmea2log-remarks.php"]
        php = archive.read("nmea2log-remarks/nmea2log-remarks.php")
    assert php.startswith(b"<?php") and b"\r\n" not in php


def test_manifest_matches_its_zip_and_the_plugin_version():
    manifest = json.loads((PLUGIN_FOLDER / "update.json").read_text(encoding="utf-8"))
    package = (PLUGIN_FOLDER / "nmea2log-remarks.zip").read_bytes()
    version = re.search(r"^\s*\*\s*Version:\s*(\S+)\s*$", (PLUGIN_FOLDER / "nmea2log-remarks.php").read_text(encoding="utf-8"), re.MULTILINE).group(1)

    assert manifest["sha256"] == hashlib.sha256(package).hexdigest()
    assert manifest["version"] == version
    assert manifest["package"].startswith("https://raw.githubusercontent.com/Ayuus/nmea2log/main/wordpress-plugin/")


def test_building_twice_gives_the_same_bytes():
    first, _ = _builder().build()
    second, _ = _builder().build()

    assert first == second
