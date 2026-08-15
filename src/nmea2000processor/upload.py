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
from typing import Optional


class UploadError(Exception):
    pass


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
    if not key_file.exists():
        raise UploadError(f"SSH key not found: {key_file}")

    # Batch mode (-b) instead of passing the "put" command as an argument: it's the documented
    # way to script the OpenSSH sftp client non-interactively, and avoids any shell-quoting
    # concerns with paths that contain spaces.
    #
    # sftp's own batch-file parser treats backslash as an escape character in its "put"
    # arguments (like a shell would), so a Windows path's backslashes silently vanish instead of
    # being treated as path separators -- found in practice: "C:\Users\...\logbook.html" turned
    # into "C:UsersLogbook.html", which then failed with "No such file or directory". Windows
    # accepts forward slashes just as well, so using those in the batch file sidesteps the whole
    # escaping question.
    local_str = str(local_path).replace("\\", "/")
    with tempfile.NamedTemporaryFile("w", suffix=".sftp-batch", delete=False, encoding="utf-8") as handle:
        handle.write(f'put "{local_str}" "{remote_path}"\n')
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

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "unknown sftp error").strip()
        raise UploadError(message)
