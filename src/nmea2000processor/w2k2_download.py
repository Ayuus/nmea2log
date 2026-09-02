"""Downloads EBL log files from an Actisense W2K-2 via its local web API.

The W2K-2's IP address is never configured -- it's found automatically every run by scanning
this machine's own local /24 subnet for a host whose web interface identifies itself as
Actisense/W2K-2 (see ``discover_w2k2``). This only works while this machine is on the same wifi
network as the W2K-2 (either joined to its own access point, or both joined to the same boat/
home wifi router).

Validated API structure (observed in browser DevTools, firmware web app; not an official API,
can change with firmware updates):

    GET  /api/data_logs
         -> {"dataFolders": [{"name": "EBL000001", "size": ...}, ...]}
    GET  /api/data_logs?method=fileList&folder=EBL000001
         -> {"dataFiles": [{"file_name": "000001_000.ebl",
                            "file_size": 5000496,
                            "file_time": 1785491054}, ...]}
         file_time = Unix epoch (seconds). Value 315532816 (= 1980-01-01) means: logged
         without GPS time on the bus (the device's default clock).
    GET  /api/download?file_name=/sdcard/logs/ebl_data_logs/<folder>/<file>
         -> binary content of the log file.
    Auth: header "Authorization: Bearer <token>", obtained via POST /api/login with JSON body
         {"user": "...", "password": "..."}.

Uses only the standard library (``urllib``), not ``requests``, so the app stays
dependency-free.

Configuration via an INI file instead of typing in credentials every time. By default
``nmea2log.ini`` is looked up in the current directory (usually the project directory) -- that
file is in ``.gitignore`` and is therefore never committed. Note: this project directory does
live in OneDrive, so a password here syncs along to the cloud/your other PC. If you don't want
that, pass a path outside OneDrive via ``--config``.

Usage:
    nmea2log-download                       # reads ./nmea2log.ini
    nmea2log-download --config path/to.ini
"""

from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DEFAULT_CONFIG_PATH, load_section
from .log import log

SD_LOG_ROOT = "/sdcard/logs/ebl_data_logs"

TIMEOUT = 30  # seconds per API request
DOWNLOAD_TIMEOUT = 300  # more headroom for the ~5 MB files

# The boat's own wifi hotspot can drop out mid-transfer -- retry a couple of times before giving
# up on a single file rather than aborting the whole remaining download queue over one transient
# reset (found in practice: ConnectionResetError mid-download crashed the entire run).
_DOWNLOAD_MAX_RETRIES = 2
_DOWNLOAD_RETRY_DELAY_S = 3.0

# Below this, the very last file (the one the W2K-2 is actively still writing to right now, see
# main()) is skipped for this run rather than downloaded -- avoids fetching a barely-started
# snapshot that's just going to be superseded by a bigger one on the next run anyway. Only ever
# applied to that one file: every other file is provably already closed out (a newer one exists
# after it), regardless of how small it ended up.
_MIN_ACTIVE_FILE_SIZE_BYTES = 500_000

# file_time value the W2K-2 uses when there was no GPS time (1980-01-01). 10-year margin:
# anything before 1990 is treated as "no real time".
NO_GPS_TIME_BEFORE = 631152000  # 1990-01-01 UTC

# Candidate field names for the token in the login response; not yet 100% confirmed which one
# the W2K-2 uses, so we try the common variants.
_TOKEN_KEYS = ("token", "bearer", "auth_token", "access_token", "sessionToken", "session")

# Discovery: scan this machine's own /24 subnet for the W2K-2's web interface on port 80.
_DISCOVERY_PORT = 80
_DISCOVERY_CONNECT_TIMEOUT = 0.6  # per host -- short, since most of the 254 addresses are unused
_DISCOVERY_HTTP_TIMEOUT = 2.0
_DISCOVERY_MAX_WORKERS = 100  # scans the whole /24 in about a second, not 254x the per-host timeout


@dataclass
class W2K2Config:
    download_dir: Path
    token: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None


def load_config(path: Optional[Path] = None) -> W2K2Config:
    """Reads the config file. Fills in missing credentials from environment variables
    (W2K2_TOKEN/W2K2_USER/W2K2_PASS/W2K2_DOWNLOAD_DIR), so the old way of working (env vars, or
    interactive login) still works too. The W2K-2's address itself is never read from here --
    see ``discover_w2k2``."""
    section: Dict[str, str] = load_section("w2k2", path or DEFAULT_CONFIG_PATH)

    download_dir = Path(
        os.environ.get("W2K2_DOWNLOAD_DIR") or section.get("download_dir") or "Actisense"
    )
    token = os.environ.get("W2K2_TOKEN") or section.get("token") or None
    user = os.environ.get("W2K2_USER") or section.get("user") or None
    password = os.environ.get("W2K2_PASS") or section.get("password") or None

    return W2K2Config(download_dir=download_dir, token=token, user=user, password=password)


def _local_subnet_prefix() -> str:
    """The first three octets of this machine's own local IPv4 address (e.g. "10.169.127."),
    found by asking the OS which local address it would use to reach the internet -- no packet is
    actually sent (UDP is connectionless), so this works offline too, as long as a default route
    exists."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0].rsplit(".", 1)[0] + "."


def _looks_like_w2k2(ip: str) -> bool:
    try:
        with socket.create_connection((ip, _DISCOVERY_PORT), timeout=_DISCOVERY_CONNECT_TIMEOUT):
            pass
    except OSError:
        return False
    try:
        request = urllib.request.Request(
            f"http://{ip}/", headers={"User-Agent": "nmea2log-discovery/0.1"}
        )
        with urllib.request.urlopen(request, timeout=_DISCOVERY_HTTP_TIMEOUT) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return "actisense" in body.lower() or "w2k" in body.lower()


def discover_w2k2() -> Optional[str]:
    """Finds the W2K-2 on the local network by scanning this machine's own /24 subnet for a host
    whose web interface identifies itself as Actisense/W2K-2. Returns a base URL (e.g.
    "http://10.169.127.101"), or None if nothing matched."""
    prefix = _local_subnet_prefix()
    hosts = [f"{prefix}{i}" for i in range(1, 255)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=_DISCOVERY_MAX_WORKERS) as executor:
        for ip, found in zip(hosts, executor.map(_looks_like_w2k2, hosts)):
            if found:
                return f"http://{ip}"
    return None


class _Session:
    """Minimal HTTP session on top of urllib: keeps the bearer token and sets it on every
    request, just like ``requests.Session`` would."""

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


def make_session(host: str, config: W2K2Config) -> _Session:
    session = _Session(host)
    if config.token:
        session.token = config.token
        return session

    user = config.user or input("Username: ")
    password = config.password or getpass.getpass("Password: ")
    body = session.post_json("/api/login", {"user": user, "password": password})
    token = next((body[k] for k in _TOKEN_KEYS if body.get(k)), None)
    if not token:
        sys.exit(
            "[error] login succeeded but no token found in the response; add the correct "
            f"field name to _TOKEN_KEYS. Response: {body}"
        )
    session.token = token
    log("[ok] logged in, token received")
    return session


def get_folders(session: _Session) -> List[dict]:
    body = session.get_json("/api/data_logs")
    folders = body.get("dataFolders", [])
    log(f"[info] {len(folders)} folder(s): " + ", ".join(f["name"] for f in folders))
    return folders


def get_files(session: _Session, folder: str) -> List[dict]:
    body = session.get_json("/api/data_logs", {"method": "fileList", "folder": folder})
    files = body.get("dataFiles", [])
    total_mb = sum(f["file_size"] for f in files) / 1e6
    log(f"[info] {folder}: {len(files)} file(s), {total_mb:.0f} MB total")
    return files


def _needs_download(local: Path, remote_size: int) -> bool:
    """Only download what we don't (fully) have yet. The size comparison also catches the case
    where the last file on the W2K-2 was still growing when we fetched it earlier."""
    return not local.exists() or local.stat().st_size != remote_size


def download_file(
    session: _Session, download_dir: Path, folder: str, info: dict, *, skip_if_growing: bool = False
) -> None:
    target_dir = download_dir / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / info["file_name"]

    if skip_if_growing and info["file_size"] < _MIN_ACTIVE_FILE_SIZE_BYTES:
        log(
            f"[skip] {folder}/{info['file_name']} still growing ({info['file_size']} bytes) "
            "-- waiting for a later run"
        )
        return

    if not _needs_download(target, info["file_size"]):
        log(f"[skip] {folder}/{info['file_name']} already complete locally")
        return

    for attempt in range(_DOWNLOAD_MAX_RETRIES + 1):
        if attempt:
            time.sleep(_DOWNLOAD_RETRY_DELAY_S)
        try:
            session.download_to(
                "/api/download", {"file_name": f"{SD_LOG_ROOT}/{folder}/{info['file_name']}"}, target
            )
            break
        # OSError alongside URLError: a dropped wifi connection to the W2K-2 mid-transfer surfaces
        # as a raw ConnectionResetError, not wrapped in URLError (found in practice, same as the
        # Overpass geocoding calls -- see geocode.py).
        except (urllib.error.URLError, OSError) as exc:
            if attempt == _DOWNLOAD_MAX_RETRIES:
                raise
            log(
                f"[warning] download of {folder}/{info['file_name']} failed ({exc}) "
                f"-- attempt {attempt + 1}/{_DOWNLOAD_MAX_RETRIES + 1}, retrying...",
                file=sys.stderr,
            )

    file_time = info.get("file_time", 0)
    if file_time > NO_GPS_TIME_BEFORE:
        os.utime(target, (file_time, file_time))
        stamp = datetime.fromtimestamp(file_time, tz=timezone.utc).isoformat()
    else:
        stamp = "NO GPS TIME (1980 stamp)"
    log(f"[ok] {folder}/{info['file_name']} ({info['file_size']} bytes, {stamp})")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nmea2log-download",
        description="Download EBL log files from an Actisense W2K-2 via its web API.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to the INI config file (default: {DEFAULT_CONFIG_PATH})",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = load_config(args.config)

    try:
        host = discover_w2k2()
    except OSError as exc:
        # _local_subnet_prefix() asks the OS for its own outbound-routing address; that fails
        # with a raw OSError ("network unreachable" etc.) when there's no active network
        # connection at all, not just when the W2K-2 isn't on it (found in practice).
        sys.exit(f"[error] network: {exc} -- make sure this device is connected to a network")
    if host is None:
        sys.exit(
            "[error] Could not find a W2K-2 on the local network -- make sure this device is "
            "connected to the same wifi network as the W2K-2 (either its own access point, or "
            "the boat/home wifi router it's joined to as a client)."
        )
    log(f"[info] Found W2K-2 at {host}", file=sys.stderr)

    try:
        session = make_session(host, config)
        folders = get_folders(session)
        # The very last file in the very last folder is the one the W2K-2 is presumably still
        # actively writing to right now -- every other file is provably already closed out (a
        # newer one exists after it), so the size-based skip only ever applies to that one.
        last_folder_name = max((f["name"] for f in folders), default=None)
        for folder in folders:
            files = get_files(session, folder["name"])
            is_last_folder = folder["name"] == last_folder_name
            last_file_name = max((f["file_name"] for f in files), default=None) if is_last_folder else None
            for info in files:
                skip_if_growing = is_last_folder and info["file_name"] == last_file_name
                download_file(session, config.download_dir, folder["name"], info, skip_if_growing=skip_if_growing)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            sys.exit(
                "[error] 401: token expired or invalid credentials -- check your config file "
                "or get a fresh token"
            )
        sys.exit(f"[error] HTTP {exc.code}: {exc}")
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(f"[error] network: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
