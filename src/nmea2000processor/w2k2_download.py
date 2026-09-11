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
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import DEFAULT_CONFIG_PATH, load_section
from .log import log, set_log_level

SD_LOG_ROOT = "/sdcard/logs/ebl_data_logs"


class DownloadCancelled(Exception):
    """Raised (by _Session.download_to() and download_file()) when a caller-supplied
    should_cancel() callback returns True -- deliberately NOT a subclass of OSError/URLError, so
    it isn't caught by download_file()'s own retry-on-transient-failure loop and instead
    propagates straight up. Only ever raised when should_cancel is explicitly passed (Android's
    sync_from_w2k2(), see android_entry.py) -- the desktop CLI never passes one, so this never
    happens there."""

TIMEOUT = 30  # seconds per API request
DOWNLOAD_TIMEOUT = 300  # more headroom for the ~5 MB files

# A real ~5 MB file over the boat's hotspot normally takes well under a minute -- generous
# headroom, but a single file still not done past this is more likely being starved to a crawl by
# something (found in practice: the phone's own hotspot deprioritizing its host app's own traffic
# while a different connected client -- e.g. a laptop also downloading at the same time -- stays
# busy) than genuinely still progressing. DOWNLOAD_TIMEOUT alone doesn't catch this: it's a
# per-read socket timeout, so a connection that's still trickling *some* bytes through every so
# often -- just far too slowly to ever realistically finish -- never triggers it, and the transfer
# can hang for many minutes with no error at all (found in practice: 30+ minutes, no exception,
# no progress, no way to notice short of watching the log for a stalled timestamp).
_MAX_DOWNLOAD_SECONDS = 120

# The boat's own wifi hotspot can drop out mid-transfer -- retry a couple of times before giving
# up on a single file rather than aborting the whole remaining download queue over one transient
# reset (found in practice: ConnectionResetError mid-download crashed the entire run).
_DOWNLOAD_MAX_RETRIES = 2
_DOWNLOAD_RETRY_DELAY_S = 3.0

# Every folder the W2K-2 reports except the current (last) one is already closed out and always
# ends up holding exactly this many files (observed in practice) -- used by build_download_plan()
# to skip a folder's own file-list request entirely once we already have this many locally.
_FILES_PER_FOLDER = 100

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


def _is_private_ipv4(octets: List[str]) -> bool:
    first, second = int(octets[0]), int(octets[1])
    return first == 10 or (first == 172 and 16 <= second <= 31) or (first == 192 and second == 168)


# Adapter description/alias substrings (case-insensitive) that mean "not a real wifi/ethernet
# link to an actual physical network" -- deprioritized, not excluded outright, in
# _windows_private_subnet_prefixes() below: still tried if nothing else works, just last.
_VIRTUAL_ADAPTER_HINTS = (
    "virtual", "hyper-v", "vmware", "virtualbox", "loopback", "tap-", "tunnel", "vpn",
)


def _windows_private_subnet_prefixes() -> List[str]:
    """Every /24 this machine currently has a private IPv4 address on, via Windows'
    Get-NetIPConfiguration (PowerShell) -- real adapters (wifi/ethernet) ordered before
    virtual-looking ones (Hyper-V, VMware, VPN, ...), so those get tried first, but nothing is
    ever silently excluded.

    Found in practice, on a real dev machine: this whole discovery mechanism assumed "there's
    only ever one active network" (see this module's own docstring), which broke the moment a
    Hyper-V virtual switch was also up -- both the internet-route trick and its own broadcast
    fallback (see _local_subnet_prefix()) can each independently end up picking whichever
    interface Windows treats as "primary" when there's no real default route to disambiguate by
    (nothing connected to the W2K-2's own isolated hotspot ever has one), with no guarantee that's
    the same interface actually joined to the W2K-2's own network. Enumerating every candidate
    and trying each one (see discover_w2k2()) sidesteps the guessing entirely -- costs at most a
    few extra seconds (each /24 scan takes about one, see _DISCOVERY_MAX_WORKERS), and is
    reliable regardless of which adapter Windows happens to prefer internally.

    Returns an empty list (never raises) on anything going wrong -- no PowerShell on PATH, not
    running on Windows at all, unexpected/unparseable output -- so callers can always fall back to
    the single-best-guess chain in _local_subnet_prefix() instead."""
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-NetIPConfiguration | Where-Object { $_.IPv4Address } | ForEach-Object { "
                "[PSCustomObject]@{ Alias = $_.InterfaceAlias; "
                "Description = $_.InterfaceDescription; IPv4 = $_.IPv4Address.IPAddress } } "
                "| ConvertTo-Json -Compress",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        parsed = json.loads(result.stdout)
        entries = parsed if isinstance(parsed, list) else [parsed]  # a single match isn't a list
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []

    real: List[str] = []
    virtual: List[str] = []
    seen: set = set()
    for entry in entries:
        ip = entry.get("IPv4")
        if not isinstance(ip, str):
            continue
        octets = ip.split(".")
        if len(octets) != 4 or not _is_private_ipv4(octets):
            continue  # also excludes link-local (169.254.x.x) and loopback
        prefix = ".".join(octets[:3]) + "."
        if prefix in seen:
            continue
        seen.add(prefix)
        haystack = f"{entry.get('Alias', '')} {entry.get('Description', '')}".lower()
        (virtual if any(hint in haystack for hint in _VIRTUAL_ADAPTER_HINTS) else real).append(prefix)
    return real + virtual


def _local_subnet_prefix() -> str:
    """The first three octets of this machine's own local IPv4 address (e.g. "10.169.127."),
    found by asking the OS which local address it would use to reach the internet -- no packet is
    actually sent (UDP is connectionless), so this works offline too, as long as a default route
    exists.

    Found in practice, on a real Windows PC: connected only to the W2K-2's own isolated access
    point (no internet, no gateway at all) -- exactly the situation this needs to work in -- that
    "reach the internet" trick itself needs a matching route to exist in the first place, and
    raises a raw OSError ("network unreachable", WinError 10051) when there isn't one.

    First fallback: the same UDP-connect trick, but targeting the broadcast address
    (255.255.255.255) instead of a specific public one -- broadcast doesn't need an actual route
    table entry (no gateway required), the OS just picks whichever interface it treats as primary
    for broadcast traffic, which is correct here since (as this module's own module docstring
    already assumes) there's only ever one active network on desktop.

    Second fallback, if even that somehow doesn't work: gethostbyname_ex(gethostname()), asking
    the OS directly for this machine's own configured addresses -- no route/broadcast capability
    needed at all, but on Windows specifically this can return a stale or unrelated address
    instead of the currently-active interface's own one (found in practice: came back with
    nothing usable on the very network this whole fallback chain exists for), so it's kept last,
    strictly as a last resort behind the more reliable broadcast trick above.

    Every fallback only runs once the one before it has already failed, so this costs nothing on
    a normal connection with real internet access."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0].rsplit(".", 1)[0] + "."
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.connect(("255.255.255.255", 1))
            return sock.getsockname()[0].rsplit(".", 1)[0] + "."
    except OSError:
        pass

    _, _, addresses = socket.gethostbyname_ex(socket.gethostname())
    for address in addresses:
        octets = address.split(".")
        if len(octets) == 4 and _is_private_ipv4(octets):
            return ".".join(octets[:3]) + "."
    raise OSError(
        "[nmea2log] could not determine this machine's own local IPv4 address by any means "
        "(no internet route, broadcast, or resolvable private hostname address)"
    )


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


def _candidate_subnet_prefixes() -> List[str]:
    """Every /24 worth scanning, in order -- the structured, multi-adapter-aware enumeration
    first (see _windows_private_subnet_prefixes()), falling back to the single best guess (see
    _local_subnet_prefix()) only when that returned nothing at all (not on Windows, no
    PowerShell on PATH, unparseable output, ...)."""
    prefixes = _windows_private_subnet_prefixes()
    return prefixes if prefixes else [_local_subnet_prefix()]


def discover_w2k2(subnet_prefix: Optional[str] = None) -> Optional[str]:
    """Finds the W2K-2 on the local network by scanning a /24 subnet for a host whose web
    interface identifies itself as Actisense/W2K-2. Returns a base URL (e.g.
    "http://10.169.127.101"), or None if nothing matched anywhere.

    By default (subnet_prefix=None) every subnet this machine currently has a private IPv4
    address on gets tried in turn (see _candidate_subnet_prefixes()) -- found in practice, a real
    dev machine with a Hyper-V virtual switch also up: guessing just one "the" local subnet isn't
    reliable once more than one adapter is active at once, which desktop can no longer assume
    never happens. On Android, where the hotspot and the cellular uplink are both active at once
    but self-detection has no equivalent way to enumerate adapters, callers pass the hotspot's
    own subnet explicitly (from NetworkInterface enumeration) to skip this entirely -- an explicit
    subnet_prefix is always scanned alone, never combined with anything else.
    """
    prefixes = [subnet_prefix] if subnet_prefix is not None else _candidate_subnet_prefixes()
    for prefix in prefixes:
        log(f"[info] Scanning {prefix}0/24 for a W2K-2...", file=sys.stderr)
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

    def download_to(
        self,
        path: str,
        params: Dict[str, str],
        target: Path,
        *,
        expected_size: Optional[int] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Downloads to ``target``, resuming from wherever a previous, interrupted attempt at the
        exact same ``target`` left off (via an HTTP Range request) instead of always restarting
        from byte 0 -- found in practice, on a real, severely bandwidth-limited connection to the
        W2K-2 (well under 10 KB/s in one real case): a multi-megabyte file could never complete at
        all if every retry (see download_file()) threw away the previous attempt's own progress
        and started over, since each attempt's own share of that progress alone was smaller than
        the whole file. An .ebl file only ever grows on the device, never shrinks or rewrites
        already-written bytes (see _needs_download()'s own reasoning), so a partial file from any
        earlier attempt -- this run's own retry, or even a previous run's leftover -- is always a
        genuine, safe-to-build-on prefix of the final file.

        Falls back to a full restart if the server doesn't actually honor the Range request (some
        HTTP status other than 206 Partial Content) rather than assuming it did -- appending fresh
        full-file content after an existing partial would otherwise silently corrupt the file."""
        url = self.base_url + path + "?" + _urlencode(params)
        resume_from = target.stat().st_size if target.exists() else 0
        headers = self._headers()
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
        request = urllib.request.Request(url, headers=headers)
        start = time.monotonic()
        try:
            response = urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and resume_from:
                # The existing partial doesn't align with what the server has any more (e.g. it
                # was replaced/rotated between attempts, or the local leftover is stale/corrupt)
                # -- discard it and restart this same call from scratch instead of retrying an
                # identical, permanently-416ing request forever.
                target.unlink(missing_ok=True)
                return self.download_to(
                    path, params, target, expected_size=expected_size, should_cancel=should_cancel
                )
            raise
        with response:
            resumed = bool(resume_from) and response.status == 206
            received = resume_from if resumed else 0
            with target.open("ab" if resumed else "wb") as fh:
                while True:
                    # Checked per chunk, not just once before the request -- otherwise cancelling
                    # mid-transfer of a single large/slow file (real ones take 30+ seconds, found
                    # in practice) wouldn't take effect until that whole transfer finished anyway.
                    # Leaves a partial file on disk to resume from next attempt, same as any other
                    # interrupted download.
                    if should_cancel is not None and should_cancel():
                        raise DownloadCancelled()
                    # A wall-clock cap on the whole transfer, not just DOWNLOAD_TIMEOUT's per-read
                    # socket timeout -- see _MAX_DOWNLOAD_SECONDS for why the two catch different
                    # failure modes. TimeoutError is a plain OSError subclass, so download_file()'s
                    # existing "except (urllib.error.URLError, OSError)" retry/give-up handling
                    # already covers this without needing its own case. Reports how many bytes had
                    # actually arrived by then (including anything resumed from before this
                    # attempt) -- found in practice, needed to tell apart a fully stalled
                    # connection (0 bytes) from a genuinely slow one still making real progress,
                    # which is exactly what justified adding this resume support in the first
                    # place -- without this, that distinction was unanswerable after the fact,
                    # since only the final attempt's own partial file even survives long enough to
                    # inspect, and this run doesn't log to nmea2log.log at all (see the module
                    # docstring).
                    if time.monotonic() - start > _MAX_DOWNLOAD_SECONDS:
                        size_note = f"{received} of {expected_size}" if expected_size is not None else str(received)
                        raise TimeoutError(
                            f"no full file after {_MAX_DOWNLOAD_SECONDS}s ({size_note} bytes "
                            "received) -- giving up on this attempt"
                        )
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)


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


def _will_download(target: Path, info: dict) -> bool:
    """The same gating logic download_file() itself uses to decide whether to fetch a file --
    factored out so main()'s pre-download summary can report an accurate count without a second,
    separately-maintained copy of this condition."""
    return _needs_download(target, info["file_size"])


def download_file(
    session: _Session,
    download_dir: Path,
    folder: str,
    info: dict,
    *,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> None:
    target_dir = download_dir / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / info["file_name"]

    if not _will_download(target, info):
        # debug, not info: on a normal day-to-day sync this fires for every already-downloaded
        # file in the folder being checked -- easily a couple thousand lines with zero new
        # information, drowning out the one summary line (build_download_plan()'s own "N of M
        # file(s) still need downloading") that actually says anything. Still written to the log
        # file in full (see log()), just not shown live/forwarded to the Android UI.
        log(f"[skip] {folder}/{info['file_name']} already complete locally", level="debug")
        return

    if should_cancel is not None and should_cancel():
        raise DownloadCancelled()

    for attempt in range(_DOWNLOAD_MAX_RETRIES + 1):
        if attempt:
            time.sleep(_DOWNLOAD_RETRY_DELAY_S)
        try:
            session.download_to(
                "/api/download",
                {"file_name": f"{SD_LOG_ROOT}/{folder}/{info['file_name']}"},
                target,
                expected_size=info["file_size"],
                should_cancel=should_cancel,
            )
            actual_size = target.stat().st_size
            # >= rather than == : an .ebl file only ever grows or stays the same, never shrinks,
            # so downloading at least as many bytes as the folder listing (fetched slightly
            # earlier, in build_download_plan()) reported is always fine -- for a closed/static
            # file this is just an exact match, and for the one file that's still actively being
            # written on the device (found in practice: not always just the very last file in the
            # very last folder, as previously assumed) it means "grew a bit more between the
            # listing and the download", not an error. Only fewer bytes than expected is retried
            # below -- that's the one direction a legitimate transfer can never produce.
            if actual_size >= info["file_size"]:
                break
            # The transfer completed with no error (a clean EOF, no exception from download_to())
            # but ended up short -- found in practice: the W2K-2 silently served a quarter of a
            # file's real bytes for a file its own folder listing had just reported the full size
            # of, and this got logged as a plain "[ok]" success since nothing here checked the
            # actual result against what was expected. Treated the same as any other failed
            # attempt: retried, and if it's still wrong after the normal retry budget, the short
            # file is deleted (not left silently miscounted as "present" by anything that only
            # checks existence, e.g. the folder-already-complete check above) -- the next run's
            # own _needs_download() size check would have caught it anyway, but there's no reason
            # to wait for a whole extra run when we already know it's wrong right now.
            if attempt == _DOWNLOAD_MAX_RETRIES:
                target.unlink(missing_ok=True)
                log(
                    f"[warning] {folder}/{info['file_name']} downloaded {actual_size} bytes, "
                    f"expected at least {info['file_size']} -- giving up after "
                    f"{_DOWNLOAD_MAX_RETRIES + 1} attempt(s), will retry next run",
                    file=sys.stderr,
                )
                return
            log(
                f"[warning] {folder}/{info['file_name']} downloaded {actual_size} bytes, expected "
                f"at least {info['file_size']} -- attempt {attempt + 1}/{_DOWNLOAD_MAX_RETRIES + 1}, "
                "retrying...",
                file=sys.stderr,
            )
            continue
        # A 404 gets the same number of retries as any other error below -- found in practice
        # that it isn't always "file genuinely gone" (the W2K-2's simple embedded web server can
        # apparently return a spurious 404 under concurrent load, e.g. Android and the desktop
        # CLI both hitting it at once, for a file that's still really there). Only treated as
        # "gone, skip it and keep going" if it's still 404 after exhausting the normal retry
        # budget -- never fatal to the rest of the run either way, unlike other errors.
        except urllib.error.HTTPError as exc:
            if attempt == _DOWNLOAD_MAX_RETRIES:
                if exc.code == 404:
                    # By this point target is guaranteed either absent or a truncated leftover
                    # from an earlier attempt in this same loop (an attempt only ever reaches the
                    # size check, and thus `break`s out having matched, once it fully succeeds --
                    # found in practice: a 404 on attempt 2/3 happens inside urlopen(), before
                    # target.open("wb") is ever reached that attempt, so a short file written by
                    # attempt 1 was being left on disk here, silently miscounted as "present" by
                    # anything that only checks existence even though "skipping" was logged).
                    target.unlink(missing_ok=True)
                    log(
                        f"[skip] {folder}/{info['file_name']} still 404 after "
                        f"{_DOWNLOAD_MAX_RETRIES + 1} attempts -- skipping for this run"
                    )
                    return
                # Non-404 HTTP error, final attempt: same cleanup, then still raise (unlike a
                # persistent 404, this is fatal to the rest of the run).
                target.unlink(missing_ok=True)
                raise
            log(
                f"[warning] download of {folder}/{info['file_name']} failed ({exc}) "
                f"-- attempt {attempt + 1}/{_DOWNLOAD_MAX_RETRIES + 1}, retrying...",
                file=sys.stderr,
            )
        # OSError alongside URLError: a dropped wifi connection to the W2K-2 mid-transfer surfaces
        # as a raw ConnectionResetError, not wrapped in URLError (found in practice, same as the
        # Overpass geocoding calls -- see geocode.py); TimeoutError (see download_to()'s own wall-
        # clock cap) is also a plain OSError subclass. DownloadCancelled deliberately isn't caught
        # here -- it's not a transient failure to retry, it propagates straight up.
        except (urllib.error.URLError, OSError) as exc:
            if attempt == _DOWNLOAD_MAX_RETRIES:
                # Non-fatal, same as a persistent 404 above -- found in practice, a real bug: this
                # used to re-raise here, which aborted build_download_plan()'s *entire* remaining
                # queue over one single file's own bad transfer (a real, otherwise-fine wifi link
                # to the W2K-2 having one slow/dropped transfer among dozens of files is normal,
                # not a sign every other file would fail too). Same cleanup as the 404 give-up path
                # above: target may be a truncated leftover from an earlier attempt in this same
                # loop, not touched by this attempt (which failed before ever reaching
                # target.open("wb")) -- delete it rather than leave a wrong-size file a size-only
                # presence check can't tell apart from a real one. Left for _needs_download() to
                # pick back up next run, same as any other file that didn't get downloaded now.
                target.unlink(missing_ok=True)
                log(
                    f"[warning] download of {folder}/{info['file_name']} failed ({exc}) -- giving "
                    f"up after {_DOWNLOAD_MAX_RETRIES + 1} attempt(s), will retry next run",
                    file=sys.stderr,
                )
                return
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
    # Stays at info, unlike the "[skip] ... already complete locally" line above -- found in
    # practice, immediately: with both at debug there was no visible progress signal at all during
    # an actual download (only the totals logged once up front), which for a real multi-file
    # download (easily minutes long) reads exactly like a hang. "[skip]" is pure noise (every
    # already-local file, every run); a genuinely new download is the actual work happening.
    log(f"[ok] {folder}/{info['file_name']} ({info['file_size']} bytes, {stamp})")


def build_download_plan(
    session: _Session, download_dir: Path, *, should_cancel: Optional[Callable[[], bool]] = None
) -> Tuple[List[Tuple[str, dict]], List[dict]]:
    """Fetches every folder's file list and returns (plan, to_download):

    - plan: every (folder_name, info) pair, in remote order.
    - to_download: the subset of those `info` dicts _will_download() says will actually be
      fetched -- for a pre-download summary/progress total.

    Shared by main()'s pre-download summary and android_entry.sync_from_w2k2()'s progress
    reporting, so both track the same set of files without a second, separately-maintained copy of
    the plan.

    should_cancel, if given, is checked before each folder's own file-list request -- listing
    every folder can itself take real time (dozens of folders, one request each), so without this
    a cancelled Android sync would still have to wait out this whole phase before stopping.

    Every folder except the very last one is provably closed out already and always ends up
    holding exactly _FILES_PER_FOLDER files -- so if we already have that many locally for a given
    folder, there is nothing left it could still need from the device, and its own file-list
    request is skipped entirely (found in practice: this is most folders, most runs, and each one
    is a real HTTP round trip to the W2K-2's own simple embedded web server). A non-last folder
    with *some* but fewer than that many files present locally logs a warning instead of being
    skipped -- still checked against the device, since we can't know which specific files are
    missing without asking. The very last folder's own last file is always downloaded too, even
    though it may still be actively growing on the device (found in practice: a folder-closed-out
    assumption doesn't always hold, and a file that turns out to still be short gets naturally
    retried next run anyway by download_file()'s own size check -- an occasional wasted redownload
    beats silently sitting on stale/incomplete data, asked for explicitly)."""
    folders = get_folders(session)
    last_folder_name = max((f["name"] for f in folders), default=None)
    plan: List[Tuple[str, dict]] = []
    for folder in folders:
        if should_cancel is not None and should_cancel():
            raise DownloadCancelled()
        folder_name = folder["name"]
        is_last_folder = folder_name == last_folder_name

        local_dir = download_dir / folder_name
        local_count = len(list(local_dir.glob("*.ebl"))) if local_dir.is_dir() else 0
        if not is_last_folder and local_count >= _FILES_PER_FOLDER:
            continue
        if not is_last_folder and 0 < local_count < _FILES_PER_FOLDER:
            log(
                f"[warning] {folder_name}: only {local_count} of the expected {_FILES_PER_FOLDER} "
                "file(s) present locally -- checking with the device"
            )

        files = get_files(session, folder_name)
        folder_needs = 0
        for info in files:
            plan.append((folder_name, info))
            if _will_download(download_dir / folder_name / info["file_name"], info):
                folder_needs += 1
        # Only when there's actually something pending -- most folders are already fully synced
        # after the first run, and logging "0 of 100" for every one of those on every subsequent
        # run would just be noise.
        if folder_needs:
            log(f"[info] {folder_name}: {folder_needs} of {len(files)} file(s) still need downloading")

    to_download = [
        info
        for folder_name, info in plan
        if _will_download(download_dir / folder_name / info["file_name"], info)
    ]
    return plan, to_download


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
        "-v", "--verbose",
        action="store_true",
        help="Show routine per-item detail too (every already-local .ebl file skipped), not "
        "just the per-run summary lines",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.verbose:
        set_log_level("debug")
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
        plan, to_download = build_download_plan(session, config.download_dir)
        total_mb = sum(info["file_size"] for info in to_download) / 1e6
        # No "of M" denominator here -- build_download_plan() now skips already-complete folders
        # entirely (see _FILES_PER_FOLDER), so len(plan) only reflects however many folders
        # happened to need checking this run, not the size of the whole archive; showing it next
        # to the download count read as a meaningful fraction when it no longer is one (asked for
        # explicitly). The per-folder "N of M" lines above this one still have real M's (that
        # folder's own full file count) and already say which folder.
        log(f"[info] {len(to_download)} file(s) need downloading ({total_mb:.0f} MB)")

        for folder_name, info in plan:
            download_file(session, config.download_dir, folder_name, info)
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
