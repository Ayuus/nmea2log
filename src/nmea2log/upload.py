"""Uploads the generated HTML logbook to a website, so it's viewable from anywhere without
running a server of your own (e.g. exposing a Raspberry Pi to the internet, with all the port-
forwarding/dynamic-DNS hassle that involves).

Two transports:

- ``upload_via_rest`` (preferred): posts the HTML straight to a WordPress REST endpoint (see
  wordpress-plugin/nmea2log-remarks.php's ``/logbook`` route), authenticated with a WordPress
  Application Password. No SSH key needed on this machine -- just reuses the same
  logboek_editor-role account this project already needs for remarks.
- ``upload_file`` (SFTP, kept as a fallback for whoever hasn't switched to the REST endpoint yet):
  the system's own ``sftp`` client (OpenSSH -- already installed on Windows 10/11 and Debian) in
  batch mode with key-based authentication, instead of adding an SFTP library dependency. Key-
  based auth is what makes this usable unpatched: the alternative, a login password, can't be
  scripted through the ``sftp`` CLI without prompting interactively, which defeats the point of
  running this automatically after every log conversion.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List


class UploadError(Exception):
    pass


_REST_ROUTE_SUFFIX = "/wp-json/nmea2log/v1/logbook"


def normalize_rest_upload_url(value: str) -> str:
    """Fills in the plugin's own fixed REST route (see wordpress-plugin/nmea2log-remarks.php's
    own ``register_rest_route('nmea2log/v1', '/logbook', ...)``) and the ``https://`` scheme when
    [value] looks like just the site's own bare address (e.g. "ayuus.com"), so entering the
    upload URL only ever needs the one thing that actually varies between sites -- asked for
    explicitly, found in practice: typing out the full
    ``https://your-site.example/wp-json/nmea2log/v1/logbook`` by hand (or trying to edit only the
    "your-site.example" part of that placeholder) is exactly the kind of fiddly, easy-to-get-wrong
    step this project otherwise tries hard to avoid.

    Also lowercases the whole result (asked for explicitly) -- a domain name is case-insensitive
    regardless, and this sidesteps a device that capitalized the first letter on the way in (a
    phone keyboard's default "capitalize the first letter of a new field" behaviour) ever silently
    changing what gets saved.

    WordPress mounts every plugin's REST routes under its own site's ``/wp-json/`` base -- that
    part is WordPress core behaviour, not something this (or any) plugin controls, so a value that
    already contains "/wp-json/" is assumed to already be a complete REST URL (typed by hand, or
    carried over from before this normalization existed) and is returned unchanged (beyond
    lowercasing) rather than risking a doubled-up path. Blank input stays blank -- nothing to fill
    in yet."""
    stripped = value.strip().lower()
    if not stripped:
        return stripped
    if "/wp-json/" in stripped:
        return stripped
    if not stripped.startswith(("http://", "https://")):
        stripped = "https://" + stripped
    return stripped.rstrip("/") + _REST_ROUTE_SUFFIX


def logbook_page_address(publish_address: str, boat_name: str) -> str:
    """Where the published logbook can be read: the site's address (from what was typed as the publish address) and the boat's
    own page on it, ``https://your-site.example/little_endian/`` for a boat called "Little Endian" -- the plugin derives that
    page from the boat name the same way (``nmea2log_slug_from_boat_name()``: WordPress's slug, with underscores). For the
    "view live site" action after a publish. Blank when no site address was typed. The site's default page (``logboek``) is
    the one without a boat name."""
    full = normalize_rest_upload_url(publish_address)
    parts = urllib.parse.urlsplit(full)
    if not full or not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}/{_boat_slug(boat_name)}/"


def _boat_slug(boat_name: str) -> str:
    """The boat name as the plugin's page name: lowercase, accents dropped, anything but letters and digits a separator
    (WordPress's ``sanitize_title``), and an underscore as separator."""
    text = unicodedata.normalize("NFKD", boat_name).encode("ascii", "ignore").decode("ascii").lower()
    text = text.replace("'", "")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "logboek"


def upload_via_rest(html_content: bytes, url: str, user: str, app_password: str) -> None:
    """Posts the built HTML logbook to a WordPress REST endpoint (see wordpress-plugin/
    nmea2log-remarks.php's ``nmea2log_logbook_upload()``) instead of over SFTP -- no SSH key
    needed on this machine, just a WordPress Application Password (Users > Profile > Application
    Passwords on the account's own profile page, not the account's real login password) for a
    user with the logboek_editor role (or Administrator).

    The raw HTML bytes are the request body, not wrapped in a JSON envelope: at several hundred
    KB, that would only add escaping overhead for no benefit, and the server side reads the raw
    body the same way. Raises ``UploadError`` with the server's own message on anything but a
    success response, same contract as ``upload_file``."""
    credentials = base64.b64encode(f"{user}:{app_password}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        url,
        data=html_content,
        method="POST",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "text/html; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            final_url = response.geturl()
    # HTTPError first: it's a URLError subclass, so the broader except below would otherwise
    # catch it too, but without the response body the server actually sent (e.g. this plugin's
    # own WP_Error message, like "Uploaded content looks incomplete") -- exactly the detail
    # worth surfacing here, same reasoning as _sftp_error_message() stripping noise so the real
    # error is what the caller actually sees.
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        raise UploadError(f"HTTP {exc.code}: {body or exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UploadError(str(exc)) from exc

    # A 2xx response alone isn't proof anything was actually uploaded -- found in practice, for
    # real: a misconfigured url pointing at the site's own page (not the REST endpoint) got a
    # 301/302 back, which urllib follows *silently*, converting the POST to a GET as it does so --
    # the login page it landed on then answered 200, with the logbook itself dropped on the floor
    # by the redirect, and this function returning normally as if it had succeeded. The plugin's
    # own success response is always exactly ``{"ok": true, "bytes": <int>}`` (see
    # nmea2log_logbook_upload() in wordpress-plugin/nmea2log-remarks.php), so anything else --
    # wrong content type, a login form, an unrelated JSON shape -- is treated as a failure here,
    # regardless of the HTTP status code that got it there.
    if final_url != url:
        raise UploadError(f"Redirected to {final_url} instead of uploading -- check the configured URL")
    try:
        result = json.loads(body)
    except ValueError:
        result = None
    if not isinstance(result, dict) or result.get("ok") is not True:
        preview = body[:200].decode("utf-8", errors="replace")
        raise UploadError(f"Unexpected response, not the plugin's own success reply -- check the configured URL: {preview!r}")


def _sftp_error_message(result: subprocess.CompletedProcess) -> str:
    """The sftp client's own error text, with the server's mandatory pre-login banner stripped
    out -- TransIP (and presumably other providers) show a multi-line legal disclaimer on every
    single connection, successful or not, which otherwise buries the actual error (e.g.
    "Connection reset by peer") under several lines of unrelated boilerplate (found in practice).
    Every banner line observed so far starts with "*", which no real sftp error line does, so
    that's used to tell them apart instead of matching the banner's exact (provider-specific)
    wording."""
    text = (result.stderr or result.stdout or "unknown sftp error").strip()
    useful_lines = [line for line in text.splitlines() if not line.strip().startswith("*")]
    cleaned = "\n".join(useful_lines).strip()
    return cleaned or text


def _run_sftp_batch(
    lines: List[str], host: str, user: str, key_file: Path, port: int
) -> subprocess.CompletedProcess:
    """Runs an SFTP client batch script (one command per line) against ``host`` in a single
    session, one attempt, no retry -- a failed logbook upload should surface immediately rather
    than silently costing the caller several seconds first."""
    if not key_file.exists():
        raise UploadError(f"SSH key not found: {key_file}")

    # Batch mode (-b) instead of passing commands as arguments: it's the documented way to
    # script the OpenSSH sftp client non-interactively, and avoids any shell-quoting concerns
    # with paths that contain spaces.
    with tempfile.NamedTemporaryFile("w", suffix=".sftp-batch", delete=False, encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
        batch_file = Path(handle.name)

    try:
        result = subprocess.run(
            [
                "sftp",
                "-i", str(key_file),
                "-P", str(port),
                "-o", "BatchMode=yes",  # fail instead of falling back to a password prompt
                # Auto-trust the host key the first time this host is ever connected to instead
                # of failing with "Host key verification failed." (BatchMode disables the normal
                # interactive "are you sure? (yes/no)" prompt too, found in practice) -- still
                # refuses to connect if a *previously trusted* host's key later changes, which is
                # what you actually want flagged (a changed key can mean a man-in-the-middle).
                "-o", "StrictHostKeyChecking=accept-new",
                "-b", str(batch_file),
                f"{user}@{host}",
            ],
            capture_output=True,
            text=True,
        )
    finally:
        batch_file.unlink(missing_ok=True)
    return result


def _local_to_sftp_path(local_path: Path) -> str:
    """sftp's own batch-file parser treats backslash as an escape character in its "put"
    arguments (like a shell would), so a Windows path's backslashes silently vanish instead of
    being treated as path separators -- found in practice: "C:\\Users\\...\\logbook.html" turned
    into "C:UsersLogbook.html", which then failed with "No such file or directory". Windows
    accepts forward slashes just as well, so using those in the batch file sidesteps the whole
    escaping question."""
    return str(local_path).replace("\\", "/")


def upload_file(
    local_path: Path,
    host: str,
    user: str,
    remote_path: str,
    key_file: Path,
    port: int = 22,
) -> None:
    """Copies ``local_path`` to ``remote_path`` on ``host`` over SFTP. Raises ``UploadError``
    with the SFTP client's own message on failure (wrong key, host unreachable, remote path
    doesn't exist, ...) instead of letting a raw ``CalledProcessError`` traceback through.

    Uploads to a temporary name first, then renames it into place, rather than writing
    ``remote_path`` directly -- a ``put`` overwriting an existing file in place is not atomic, so
    a page (e.g. the WordPress gatekeeper for the HTML logbook, which reads this file fresh on
    every single request) served to a visitor mid-upload can read a truncated/incomplete file --
    found in practice: an empty-looking trips table on the live site, with no trace of it in
    nmea2log.log since the upload itself still reported success. POSIX rename() is atomic, so a
    concurrent read now always gets either the complete old file or the complete new one."""
    remote_tmp_path = f"{remote_path}.tmp-upload"
    lines = [
        f'put "{_local_to_sftp_path(local_path)}" "{remote_tmp_path}"',
        f'rename "{remote_tmp_path}" "{remote_path}"',
    ]
    result = _run_sftp_batch(lines, host, user, key_file, port)
    if result.returncode != 0:
        raise UploadError(_sftp_error_message(result))


