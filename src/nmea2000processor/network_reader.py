"""Live inlezen van een N2K ASCII-stream via TCP, rechtstreeks van de W2K-2.

De W2K-2 fungeert zelf als TCP-server: hij biedt drie onafhankelijke "data servers" aan
(standaard poorten 60001-60003, instelbaar in de webinterface van het apparaat), elk met een
eigen protocol (TCP/UDP) en formaat. Zet één van die data servers op protocol **TCP** en
formaat **N2K ASCII**, en verbind hiermee als client naar het IP-adres en de poort van de
W2K-2 (bijvoorbeeld het access point-adres ``192.168.4.1``, of het adres dat de W2K-2 krijgt
op jouw boot-wifi/thuisnetwerk).

Deze module hergebruikt dezelfde regel-parser als het bestandsgebaseerde pad
(``ascii_reader.iter_frames_from_lines``), zodat live en achteraf verwerken zich identiek
gedragen.
"""

from __future__ import annotations

import socket
from datetime import date
from pathlib import Path
from typing import IO, Iterator, Optional, Union

from .ascii_reader import iter_frames_from_lines
from .model import Frame

DEFAULT_PORT = 60001  # standaardpoort van "Data Server 1" op de W2K-2


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
    """Verbind live met de W2K-2 over TCP en geef Frame's terug zodra ze binnenkomen.

    Maakt de verbinding meteen bij aanroep (niet pas bij de eerste iteratie), zodat een
    verbindingsfout (``OSError``) direct door de aanroeper kan worden afgevangen.

    ``tee_to``: optioneel pad waarnaar de ruwe inkomende ASCII-regels tegelijk worden
    weggeschreven (toevoegend), zodat je naast live verwerking ook een permanent logbestand
    overhoudt om later opnieuw te verwerken of te bewaren.
    """
    tee_path = Path(tee_to) if tee_to is not None else None
    sock = socket.create_connection((host, port), timeout=connect_timeout)
    sock.settimeout(None)
    return _read_frames(sock, tee_path)
