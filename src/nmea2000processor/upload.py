"""Uploads the generated HTML logbook to a website over SFTP, so it's viewable from anywhere
without running a server of your own (e.g. exposing a Raspberry Pi to the internet, with all the
port-forwarding/dynamic-DNS hassle that involves).

Uses the system's own ``sftp`` client (OpenSSH -- already installed on Windows 10/11 and Debian)
in batch mode with key-based authentication, instead of adding an SFTP library dependency. Key-
based auth is what makes this usable unpatched: the alternative, a login password, can't be
scripted through the ``sftp`` CLI without prompting interactively, which defeats the point of
running this automatically after every log conversion.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

from .log import log

# Backing up the whole .ebl archive touches the same shared-hosting SFTP server many times in a
# row, which we've seen reset the connection out of the blue -- even on the very first command of
# a brand new session (found in practice: replaying the exact same failed batch by hand right
# after succeeded instantly). Retrying the whole batch a couple of times before giving up saves a
# run from losing all its progress to what's usually just a momentary hiccup.
_BACKUP_MAX_RETRIES = 2
_BACKUP_RETRY_DELAY_S = 5.0


class UploadError(Exception):
    pass


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
    lines: List[str], host: str, user: str, key_file: Path, port: int, retries: int = 0
) -> subprocess.CompletedProcess:
    """Runs an SFTP client batch script (one command per line) against ``host`` in a single
    session -- shared by every function in this module so uploading several files only opens one
    connection instead of one per file.

    ``retries`` reruns the whole batch (a fresh connection each time) if it fails, waiting a few
    seconds in between -- see ``_BACKUP_MAX_RETRIES`` for why. Defaults to 0 (no retry) for callers
    like ``upload_file`` where a failure should surface immediately rather than silently costing
    the caller several seconds first."""
    if not key_file.exists():
        raise UploadError(f"SSH key not found: {key_file}")

    result = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(_BACKUP_RETRY_DELAY_S)

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

        if result.returncode == 0:
            break
        if attempt < retries:
            message = _sftp_error_message(result)
            # A newline after "failed", not a space -- the sftp client's own error message can
            # itself be multi-line (e.g. the server's login banner), which otherwise starts
            # awkwardly mid-line right after the prefix (found in practice, same as the other
            # sftp error messages logged elsewhere).
            log(
                f"[upload] sftp command failed (attempt {attempt + 1}/{retries + 1}, retrying...):\n{message}",
                file=sys.stderr,
            )
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


def list_remote_filenames(
    host: str,
    user: str,
    remote_dir: str,
    key_file: Path,
    port: int = 22,
) -> Set[str]:
    """Bare filenames (not full paths) already present in ``remote_dir``, so a caller backing up
    many local files can skip whichever ones are already there instead of re-uploading everything
    on every run. Returns an empty set if ``remote_dir`` doesn't exist yet (a first-ever backup),
    rather than raising -- that's just "nothing backed up here yet", not a real failure.

    Ensures ``remote_dir`` exists first (ignoring the error if it already does, same as
    ``upload_files``' own "-mkdir") instead of trying to distinguish "doesn't exist yet" from
    other ``ls`` failures by matching on the sftp client's own error text -- the OpenSSH sftp
    client doesn't expose a distinguishable exit code for that, and the text itself isn't
    consistent (found in practice: one server said "No such file or directory", another just "not
    found"). With the directory guaranteed to exist, any remaining ``ls`` failure is a real
    problem worth raising instead of silently swallowing."""
    result = _run_sftp_batch(
        [f'-mkdir "{remote_dir}"', f'ls -1 "{remote_dir}"'],
        host, user, key_file, port, retries=_BACKUP_MAX_RETRIES,
    )
    if result.returncode != 0:
        raise UploadError(_sftp_error_message(result))
    names = set()
    for line in result.stdout.splitlines():
        name = line.strip().split("/")[-1]
        if name and name not in (".", ".."):
            names.add(name)
    return names


def upload_files(
    local_paths: List[Path],
    host: str,
    user: str,
    remote_dir: str,
    key_file: Path,
    port: int = 22,
) -> None:
    """Uploads every file in ``local_paths`` into ``remote_dir`` (same basename, directory
    stripped) in a single SFTP session. Creates ``remote_dir`` first if it doesn't exist yet --
    the leading "-" makes sftp ignore the error if it already does, the documented way to get
    mkdir -p-like behaviour out of a client that doesn't have one. A no-op (no connection at all)
    for an empty list, since there's then nothing to justify even creating the directory."""
    if not local_paths:
        return
    lines = [f'-mkdir "{remote_dir}"']
    for local_path in local_paths:
        remote_path = f"{remote_dir}/{local_path.name}"
        lines.append(f'put "{_local_to_sftp_path(local_path)}" "{remote_path}"')
    result = _run_sftp_batch(lines, host, user, key_file, port, retries=_BACKUP_MAX_RETRIES)
    if result.returncode != 0:
        raise UploadError(_sftp_error_message(result))


def list_remote_filenames_multi(
    remote_dirs: List[str],
    host: str,
    user: str,
    key_file: Path,
    port: int = 22,
) -> Set[str]:
    """Like ``list_remote_filenames``, but checks every directory in ``remote_dirs`` within a
    single SFTP session instead of opening one session per directory -- found in practice: a
    --backup-ebl run touching several EBL##### folders opened one "list" session and one "upload"
    session *per folder*, and hit TransIP's occasional connection resets roughly 5x more often
    than the single-session HTML upload (41% of runs needed a retry, vs 8%) -- consistent with
    more freshly-opened connections meaning more chances to hit whatever's causing that, even
    though every file always did eventually make it across either way.

    Returns the union of filenames found across every directory: safe to merge like this because
    every backed-up file's own name already encodes which EBL##### folder it came from (e.g.
    "000015_090.ebl"), so a name found under one remote_dir is never mistaken for an unrelated,
    same-named file that actually belongs to a different one. A no-op (empty set, no connection at
    all) for an empty list."""
    if not remote_dirs:
        return set()
    lines = []
    for remote_dir in remote_dirs:
        lines.append(f'-mkdir "{remote_dir}"')
        lines.append(f'ls -1 "{remote_dir}"')
    result = _run_sftp_batch(lines, host, user, key_file, port, retries=_BACKUP_MAX_RETRIES)
    if result.returncode != 0:
        raise UploadError(_sftp_error_message(result))
    names = set()
    for line in result.stdout.splitlines():
        name = line.strip().split("/")[-1]
        if name and name not in (".", ".."):
            names.add(name)
    return names


def upload_files_to_dirs(
    local_paths_by_remote_dir: Dict[str, List[Path]],
    host: str,
    user: str,
    key_file: Path,
    port: int = 22,
) -> None:
    """Like ``upload_files``, but can target several different remote directories within a single
    SFTP session -- see ``list_remote_filenames_multi`` for why that matters. A no-op (no
    connection at all) if every directory's file list is empty."""
    if not any(local_paths_by_remote_dir.values()):
        return
    lines = []
    for remote_dir, local_paths in local_paths_by_remote_dir.items():
        if not local_paths:
            continue
        lines.append(f'-mkdir "{remote_dir}"')
        for local_path in local_paths:
            remote_path = f"{remote_dir}/{local_path.name}"
            lines.append(f'put "{_local_to_sftp_path(local_path)}" "{remote_path}"')
    result = _run_sftp_batch(lines, host, user, key_file, port, retries=_BACKUP_MAX_RETRIES)
    if result.returncode != 0:
        raise UploadError(_sftp_error_message(result))
