import socket
import threading
from pathlib import Path
from typing import List

from nmea2000processor.network_reader import iter_frames_tcp


def _serve_lines(server_sock: socket.socket, lines: List[str]) -> None:
    conn, _ = server_sock.accept()
    with conn:
        for line in lines:
            conn.sendall(line.encode("ascii"))
    server_sock.close()


def test_iter_frames_tcp_reads_lines_and_tees(tmp_path: Path):
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    host, port = server_sock.getsockname()

    lines = [
        "A173321.107 23FF7 1F513 012F3070002F30709F\n",
        "A173322.107 23FF7 1F513 00\n",
    ]
    thread = threading.Thread(target=_serve_lines, args=(server_sock, lines), daemon=True)
    thread.start()

    tee_path = tmp_path / "tee.raw"
    frames = list(iter_frames_tcp(host, port, tee_to=tee_path))
    thread.join(timeout=5)

    assert len(frames) == 2
    assert frames[0].pgn == (0x1F513 & 0x3FFFF)
    assert frames[0].source == 0x23
    assert tee_path.read_text(encoding="ascii") == "".join(lines)


def test_iter_frames_tcp_connect_error_raises_oserror():
    # There's (probably) no server running on this port on localhost.
    import pytest

    with pytest.raises(OSError):
        list(iter_frames_tcp("127.0.0.1", 1, connect_timeout=1.0))
