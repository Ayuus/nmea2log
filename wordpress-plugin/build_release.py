"""Makes the files WordPress updates this plugin from: ``nmea2log-remarks.zip`` (the plugin in a folder of its own, as WordPress
installs it) and ``update.json`` (the newest version, the zip's address and its SHA-256), next to this script.

    python wordpress-plugin/build_release.py          # after any change to nmea2log-remarks.php, with its Version raised

Both files are committed with the change: the plugin on a site asks GitHub for update.json (see the update code at the end of
nmea2log-remarks.php) and downloads the zip. tests/test_wordpress_plugin.py fails when they do not match the plugin, so a plugin
change cannot be pushed without them.

The zip is the same bytes wherever it is made (fixed timestamps, line endings as in git), which is what lets the test compare."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Tuple

FOLDER = Path(__file__).resolve().parent
PLUGIN_FILE = FOLDER / "nmea2log-remarks.php"
ZIP_FILE = FOLDER / "nmea2log-remarks.zip"
MANIFEST_FILE = FOLDER / "update.json"
PACKAGE_URL = "https://raw.githubusercontent.com/Ayuus/nmea2log/main/wordpress-plugin/nmea2log-remarks.zip"
_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


def plugin_source() -> bytes:
    """The plugin file with git's line endings (LF), whatever a checkout on this machine did to it."""
    return PLUGIN_FILE.read_bytes().replace(b"\r\n", b"\n")


def plugin_version(source: bytes) -> str:
    match = re.search(rb"^\s*\*\s*Version:\s*(\d+(?:\.\d+){1,2})\s*$", source, re.MULTILINE)
    if not match:
        raise ValueError("no 'Version:' line in the plugin header")
    return match.group(1).decode("ascii")


def build() -> Tuple[bytes, dict]:
    """The zip and the manifest for the plugin as it is now."""
    source = plugin_source()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("nmea2log-remarks/nmea2log-remarks.php", date_time=_TIMESTAMP)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(info, source)
    package = buffer.getvalue()
    manifest = {
        "version": plugin_version(source),
        "package": PACKAGE_URL,
        "sha256": hashlib.sha256(package).hexdigest(),
    }
    return package, manifest


def main() -> None:
    package, manifest = build()
    ZIP_FILE.write_bytes(package)
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {ZIP_FILE.name} and {MANIFEST_FILE.name}: version {manifest['version']}")


if __name__ == "__main__":
    main()
