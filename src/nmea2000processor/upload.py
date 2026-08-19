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
import tempfile
from pathlib import Path
from typing import List, Optional, Set


class UploadError(Exception):
    pass


def _run_sftp_batch(lines: List[str], host: str, user: str, key_file: Path, port: int) -> subprocess.CompletedProcess:
    """Runs an SFTP client batch script (one command per line) against ``host`` in a single
    session -- shared by every function in this module so uploading several files only opens one
    connection instead of one per file."""
    if not key_file.exists():
        raise UploadError(f"SSH key not found: {key_file}")

    # Batch mode (-b) instead of passing commands as arguments: it's the documented way to
    # script the OpenSSH sftp client non-interactively, and avoids any shell-quoting concerns
    # with paths that contain spaces.
    with tempfile.NamedTemporaryFile("w", suffix=".sftp-batch", delete=False, encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
        batch_file = Path(handle.name)

    try:
        return subprocess.run(
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
    doesn't exist, ...) instead of letting a raw ``CalledProcessError`` traceback through."""
    lines = [f'put "{_local_to_sftp_path(local_path)}" "{remote_path}"']
    result = _run_sftp_batch(lines, host, user, key_file, port)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "unknown sftp error").strip()
        raise UploadError(message)


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
        [f'-mkdir "{remote_dir}"', f'ls -1 "{remote_dir}"'], host, user, key_file, port
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "unknown sftp error").strip()
        raise UploadError(message)
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
    result = _run_sftp_batch(lines, host, user, key_file, port)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "unknown sftp error").strip()
        raise UploadError(message)
