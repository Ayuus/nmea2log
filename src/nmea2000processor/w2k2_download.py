"""Downloads EBL log files from an Actisense W2K-2 via its local web API.

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
import sys
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
DEFAULT_DOWNLOAD_WORKERS = 4

# file_time value the W2K-2 uses when there was no GPS time (1980-01-01). 10-year margin:
# anything before 1990 is treated as "no real time".
NO_GPS_TIME_BEFORE = 631152000  # 1990-01-01 UTC

# Candidate field names for the token in the login response; not yet 100% confirmed which one
# the W2K-2 uses, so we try the common variants.
_TOKEN_KEYS = ("token", "bearer", "auth_token", "access_token", "sessionToken", "session")


@dataclass
class W2K2Config:
    url: str
    download_dir: Path
    token: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None


def load_config(path: Optional[Path] = None) -> W2K2Config:
    """Reads the config file. Fills in missing credentials from environment variables
    (W2K2_URL/W2K2_TOKEN/W2K2_USER/W2K2_PASS/W2K2_DOWNLOAD_DIR), so the old way of working
    (env vars, or interactive login) still works too."""
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


def make_session(config: W2K2Config) -> _Session:
    session = _Session(config.url)
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


def download_file(session: _Session, download_dir: Path, folder: str, info: dict) -> None:
    target_dir = download_dir / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / info["file_name"]

    if not _needs_download(target, info["file_size"]):
        log(f"[skip] {folder}/{info['file_name']} already complete locally")
        return

    session.download_to(
        "/api/download", {"file_name": f"{SD_LOG_ROOT}/{folder}/{info['file_name']}"}, target
    )

    file_time = info.get("file_time", 0)
    if file_time > NO_GPS_TIME_BEFORE:
        os.utime(target, (file_time, file_time))
        stamp = datetime.fromtimestamp(file_time, tz=timezone.utc).isoformat()
    else:
        stamp = "NO GPS TIME (1980 stamp)"
    log(f"[ok] {folder}/{info['file_name']} ({info['file_size']} bytes, {stamp})")


def download_files(
    session: _Session, download_dir: Path, folder: str, files: List[dict], max_workers: int
) -> None:
    """Downloads one folder's files, up to ``max_workers`` at once. Each download is a separate
    HTTP request/response over its own connection, so overlapping them hides most of the
    per-request latency -- worthwhile since each ~5 MB file otherwise takes ~30s dominated by the
    W2K-2's own (slow) serving speed, not by anything on this end. ``max_workers=1`` downloads
    sequentially, e.g. if the device turns out not to handle concurrent connections well."""
    if max_workers <= 1:
        for info in files:
            download_file(session, download_dir, folder, info)
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(download_file, session, download_dir, folder, info) for info in files]
        for future in concurrent.futures.as_completed(futures):
            future.result()  # re-raise so a failed download (e.g. 401) still aborts the run


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
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_DOWNLOAD_WORKERS,
        help=f"Number of files to download concurrently per folder (default {DEFAULT_DOWNLOAD_WORKERS}). "
        "Use 1 to download sequentially, e.g. if the device can't handle concurrent connections.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = load_config(args.config)

    try:
        session = make_session(config)
        for folder in get_folders(session):
            files = get_files(session, folder["name"])
            download_files(session, config.download_dir, folder["name"], files, max_workers=args.workers)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            sys.exit(
                "[error] 401: token expired or invalid credentials -- check your config file "
                "or get a fresh token"
            )
        sys.exit(f"[error] HTTP {exc.code}: {exc}")
    except (urllib.error.URLError, TimeoutError) as exc:
        sys.exit(f"[error] network: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
