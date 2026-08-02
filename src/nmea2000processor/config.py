"""Leest instellingen uit een gedeeld INI-configbestand (standaard ``nmea2log.ini`` in de huidige
map), zodat CLI-opties niet telkens opnieuw hoeven te worden meegegeven. Expliciete
command-line-argumenten hebben altijd voorrang boven wat in het configbestand staat (zie
``cli.py``: het configbestand levert alleen de *standaardwaarden*)."""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Dict

DEFAULT_CONFIG_PATH = Path("nmea2log.ini")


def load_section(section: str, path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, str]:
    """Leest één sectie uit het configbestand. Geeft een lege dict als het bestand of de
    sectie niet bestaat (dan gelden gewoon de ingebouwde standaardwaarden)."""
    if not path.exists():
        return {}
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    if not parser.has_section(section):
        return {}
    return dict(parser[section])
