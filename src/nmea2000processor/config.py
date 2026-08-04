"""Reads settings from a shared INI config file (default ``nmea2log.ini`` in the current
directory), so CLI options don't have to be passed every time. Explicit command-line arguments
always take precedence over what's in the config file (see ``cli.py``: the config file only
supplies the *defaults*)."""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Dict

DEFAULT_CONFIG_PATH = Path("nmea2log.ini")


def load_section(section: str, path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, str]:
    """Reads a single section from the config file. Returns an empty dict if the file or the
    section doesn't exist (the built-in defaults then simply apply)."""
    if not path.exists():
        return {}
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    if not parser.has_section(section):
        return {}
    return dict(parser[section])
