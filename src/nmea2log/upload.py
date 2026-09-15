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
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import List


class UploadError(Exception):
    pass


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
            response.read()
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


