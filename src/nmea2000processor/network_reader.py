"""Read an N2K ASCII stream live over TCP, directly from the W2K-2.

The W2K-2 itself acts as a TCP server: it offers three independent "data servers" (default
ports 60001-60003, configurable in the device's web interface), each with its own protocol
(TCP/UDP) and format. Set one of those data servers to protocol **TCP** and format
**N2K ASCII**, and connect to it as a client using the W2K-2's IP address and port (for
example the access-point address ``192.168.4.1``, or the address the W2K-2 gets on your
boat wifi/home network).

This module reuses the same line parser as the file-based path
(``ascii_reader.iter_frames_from_lines``), so live and after-the-fact processing behave
identically.
"""

from __future__ import annotations

import socket
from datetime import date
from pathlib import Path
from typing import IO, Iterator, Optional, Union

from .ascii_reader import iter_frames_from_lines
from .model import Frame

DEFAULT_PORT = 60001  # default port of "Data Server 1" on the W2K-2


def _line_source(sock: socket.socket, tee_to: Optional[Path]) -> Iterator[str]:
    stream: IO[str] = sock.makefile("r", encoding="ascii", errors="replace", newline="\n")
    tee_handle = tee_to.open("a", encoding="ascii") if tee_to is not None else None
    try:
        for line in stream:
            if tee_handle is not None:
                tee_handle.write(line)
                tee_handle.flush()
            yield line
    finally:
        if tee_handle is not None:
            tee_handle.close()


def _read_frames(sock: socket.socket, tee_path: Optional[Path]) -> Iterator[Frame]:
    with sock:
        yield from iter_frames_from_lines(_line_source(sock, tee_path), date.today())


def iter_frames_tcp(
    host: str,
    port: int = DEFAULT_PORT,
    *,
    tee_to: Optional[Union[str, Path]] = None,
    connect_timeout: float = 10.0,
) -> Iterator[Frame]:
    """Connect live to the W2K-2 over TCP and yield Frames as they come in.

    Connects immediately when called (not just on the first iteration), so a connection
    error (``OSError``) can be caught by the caller right away.

    ``tee_to``: optional path to which the raw incoming ASCII lines are simultaneously
    written (appending), so alongside live processing you also end up with a permanent log
    file to reprocess or keep later.
    """
    tee_path = Path(tee_to) if tee_to is not None else None
    sock = socket.create_connection((host, port), timeout=connect_timeout)
    sock.settimeout(None)
    return _read_frames(sock, tee_path)
