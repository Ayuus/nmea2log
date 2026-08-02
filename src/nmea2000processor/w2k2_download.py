"""Downloadt EBL-logbestanden van een Actisense W2K-2 via zijn lokale web-API.

Gevalideerde API-structuur (geobserveerd in browser DevTools, firmware-webapp; geen officiële
API, kan wijzigen bij firmware-updates):

    GET  /api/data_logs
         -> {"dataFolders": [{"name": "EBL000001", "size": ...}, ...]}
    GET  /api/data_logs?method=fileList&folder=EBL000001
         -> {"dataFiles": [{"file_name": "000001_000.ebl",
                            "file_size": 5000496,
                            "file_time": 1785491054}, ...]}
         file_time = Unix-epoch (seconden). Waarde 315532816 (= 1980-01-01) betekent: gelogd
         zonder GPS-tijd op de bus (default-klok van het apparaat).
    GET  /api/download?file_name=/sdcard/logs/ebl_data_logs/<folder>/<file>
         -> binaire inhoud van het logbestand.
    Auth: header "Authorization: Bearer <token>", verkregen via POST /api/login met JSON-body
         {"user": "...", "password": "..."}.

Gebruikt alleen de standaardbibliotheek (``urllib``), geen ``requests``, zodat de app
dependency-vrij blijft.

Configuratie via een INI-bestand in plaats van steeds opnieuw inloggegevens intypen. Standaard
wordt ``nmea2log.ini`` gezocht in de huidige map (dus meestal de projectmap) — dat bestand staat
in ``.gitignore`` en wordt dus nooit gecommit. Let op: deze projectmap staat wel in OneDrive, dus
een wachtwoord hier synct mee naar de cloud/je andere pc. Wil je dat niet, geef dan een pad
buiten OneDrive op via ``--config``.

Gebruik:
    nmea2log-download                       # leest ./nmea2log.ini
    nmea2log-download --config pad/naar.ini
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DEFAULT_CONFIG_PATH, load_section

SD_LOG_ROOT = "/sdcard/logs/ebl_data_logs"

TIMEOUT = 30  # seconden per API-verzoek
DOWNLOAD_TIMEOUT = 300  # ruimer voor de ~5 MB bestanden

# file_time-waarde die de W2K-2 gebruikt als er geen GPS-tijd was (1980-01-01). Marge van 10
# jaar: alles voor 1990 beschouwen we als "geen echte tijd".
NO_GPS_TIME_BEFORE = 631152000  # 1990-01-01 UTC

# Kandidaat-veldnamen voor het token in de login-response; nog niet 100% bevestigd welke de
# W2K-2 gebruikt, dus we proberen de gangbare varianten.
_TOKEN_KEYS = ("token", "bearer", "auth_token", "access_token", "sessionToken", "session")


@dataclass
class W2K2Config:
    url: str
    download_dir: Path
    token: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None


def load_config(path: Optional[Path] = None) -> W2K2Config:
    """Leest het configbestand. Vult ontbrekende inloggegevens aan uit omgevingsvariabelen
    (W2K2_URL/W2K2_TOKEN/W2K2_USER/W2K2_PASS/W2K2_DOWNLOAD_DIR), zodat de oude manier van
    werken (env vars, of interactief inloggen) ook nog gewoon werkt."""
    section: Dict[str, str] = load_section("w2k2", path or DEFAULT_CONFIG_PATH)

    url = os.environ.get("W2K2_URL") or section.get("url") or "http://10.164.231.101"
    download_dir = Path(
        os.environ.get("W2K2_DOWNLOAD_DIR") or section.get("download_dir") or "Actisense"
    )
    token = os.environ.get("W2K2_TOKEN") or section.get("token") or None
    user = os.environ.get("W2K2_USER") or section.get("user") or None
    password = os.environ.get("W2K2_PASS") or section.get("password") or None

    return W2K2Config(url=url, download_dir=download_dir, token=token, user=user, password=password)


class _Session:
    """Minimale HTTP-sessie op basis van urllib: bewaart het bearer-token en zet 'm op elk
    verzoek, net als ``requests.Session`` dat zou doen."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token: Optional[str] = None

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json, text/plain, */*"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_json(self, path: str, params: Optional[Dict[str, str]] = None) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + _urlencode(params)
        request = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, path: str, body: Dict[str, Any]) -> Any:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8")
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))

    def download_to(self, path: str, params: Dict[str, str], target: Path) -> None:
        url = self.base_url + path + "?" + _urlencode(params)
        request = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response, target.open("wb") as fh:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                fh.write(chunk)


def _urlencode(params: Dict[str, str]) -> str:
    return urllib.parse.urlencode(params)


def make_session(config: W2K2Config) -> _Session:
    session = _Session(config.url)
    if config.token:
        session.token = config.token
        return session

    user = config.user or input("Gebruikersnaam: ")
    password = config.password or getpass.getpass("Wachtwoord: ")
    body = session.post_json("/api/login", {"user": user, "password": password})
    token = next((body[k] for k in _TOKEN_KEYS if body.get(k)), None)
    if not token:
        sys.exit(
            "[fout] login gelukt maar geen token gevonden in de response; voeg de juiste "
            f"veldnaam toe aan _TOKEN_KEYS. Response: {body}"
        )
    session.token = token
    print("[ok] ingelogd, token ontvangen")
    return session


def get_folders(session: _Session) -> List[dict]:
    body = session.get_json("/api/data_logs")
    folders = body.get("dataFolders", [])
    print(f"[info] {len(folders)} map(pen): " + ", ".join(f["name"] for f in folders))
    return folders


def get_files(session: _Session, folder: str) -> List[dict]:
    body = session.get_json("/api/data_logs", {"method": "fileList", "folder": folder})
    files = body.get("dataFiles", [])
    total_mb = sum(f["file_size"] for f in files) / 1e6
    print(f"[info] {folder}: {len(files)} bestand(en), {total_mb:.0f} MB totaal")
    return files


def _needs_download(local: Path, remote_size: int) -> bool:
    """Alleen downloaden wat we nog niet (volledig) hebben. Groottevergelijking vangt ook het
    geval dat het laatste bestand op de W2K-2 nog groeide toen we het eerder ophaalden."""
    return not local.exists() or local.stat().st_size != remote_size


def download_file(session: _Session, download_dir: Path, folder: str, info: dict) -> None:
    target_dir = download_dir / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / info["file_name"]

    if not _needs_download(target, info["file_size"]):
        print(f"[skip] {folder}/{info['file_name']} al compleet lokaal")
        return

    session.download_to(
        "/api/download", {"file_name": f"{SD_LOG_ROOT}/{folder}/{info['file_name']}"}, target
    )

    file_time = info.get("file_time", 0)
    if file_time > NO_GPS_TIME_BEFORE:
        os.utime(target, (file_time, file_time))
        stamp = datetime.fromtimestamp(file_time, tz=timezone.utc).isoformat()
    else:
        stamp = "GEEN GPS-TIJD (1980-stempel)"
    print(f"[ok] {folder}/{info['file_name']} ({info['file_size']} bytes, {stamp})")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nmea2log-download",
        description="Download EBL-logbestanden van een Actisense W2K-2 via zijn web-API.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Pad naar het INI-configbestand (standaard: {DEFAULT_CONFIG_PATH})",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = load_config(args.config)

    session = make_session(config)
    try:
        for folder in get_folders(session):
            for info in get_files(session, folder["name"]):
                download_file(session, config.download_dir, folder["name"], info)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            sys.exit(
                "[fout] 401: token verlopen of ongeldige inloggegevens -- controleer je "
                "configbestand of haal een vers token op"
            )
        sys.exit(f"[fout] HTTP {exc.code}: {exc}")
    except urllib.error.URLError as exc:
        sys.exit(f"[fout] netwerk: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
