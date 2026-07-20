"""Leest Actisense EBL-logbestanden (SD-kaartlog van de W2K-1/W2K-2, BST-95 CAN-raw-formaat).

Dit formaat is niet officieel door Actisense gepubliceerd. De framing (ESC/SOH/NL-omkadering
met byte-stuffing) en de CAN-ID-decodering hieronder zijn overgenomen van de open-source
Go-implementatie in github.com/aldas/go-nmea-client (actisense/eblreader.go) en met de hand
geverifieerd tegen de testvectoren daarin (o.a. een PGN 129025-voorbeeld dat exact naar
priority=2, pgn=129025, source=0, destination=255 decodeert).

Belangrijk verschil met het N2K ASCII-pad (``ascii_reader.py``):

1. EBL bevat **rauwe CAN-frames** (max. 8 databytes), dus PGN's die groter zijn dan 8 bytes
   (bij ons: 127489 en 127497) moeten zelf via het NMEA2000 "Fast Packet"-protocol weer in
   elkaar gezet worden — dat gebeurt hier.
2. Elk EBL-record heeft weliswaar een eigen 2-byte tijdteller, maar de betekenis daarvan is
   nergens betrouwbaar gedocumenteerd (zelfs de referentie-implementatie hierboven gokt ernaar
   en gebruikt in de praktijk gewoon de leestijd). Die teller wordt daarom hier genegeerd.
   In plaats daarvan wordt de absolute datum/tijd afgeleid uit **PGN 126992 (System Time)**,
   die zelf al in de N2K-stream zit en een volledig gedocumenteerde, ondubbelzinnige codering
   heeft. Gevolg: frames vóór de eerste 126992-boodschap in het bestand worden overgeslagen
   (er is dan nog geen tijdreferentie), en de tijdsresolutie is gelijk aan de zendfrequentie
   van PGN 126992 op jouw NMEA2000-netwerk (meestal rond de 1x/seconde).

Dit is nog niet tegen een echt EBL-bestand van een W2K-2 geverifieerd — controleer dit zodra
je een echt bestand hebt (zie README).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple, Union

from .model import Frame
from .pgn_decode import PGN_ENGINE_DYNAMIC, PGN_SYSTEM_TIME, PGN_TRIP_FUEL_ENGINE, decode_system_time

_ESC = 0x1B
_SOH = 0x01
_NL = 0x0A
_CMD_RAW_ACTISENSE_MESSAGE_RECEIVED = 0x95

# PGN's die bij ons groter zijn dan 8 bytes en dus als NMEA2000 "Fast Packet" over de bus gaan.
_FAST_PACKET_PGNS = {PGN_ENGINE_DYNAMIC, PGN_TRIP_FUEL_ENGINE}

_STATE_WAITING = 0
_STATE_READING = 1
_STATE_ESCAPING = 2


def _iter_raw_records(data: bytes) -> Iterator[bytes]:
    """Haalt ESC/SOH/NL-omkaderde records uit de ruwe bestandsbytes (met byte-stuffing)."""
    state = _STATE_WAITING
    message = bytearray()
    previous_byte: Optional[int] = None

    for current_byte in data:
        if state == _STATE_WAITING:
            if previous_byte == _ESC and current_byte == _SOH:
                state = _STATE_READING
                message = bytearray()
        elif state == _STATE_READING:
            if current_byte == _ESC:
                state = _STATE_ESCAPING
            else:
                message.append(current_byte)
        elif state == _STATE_ESCAPING:
            if current_byte == _ESC:  # dubbele ESC = letterlijke 0x1B-databyte
                message.append(current_byte)
                state = _STATE_READING
            elif current_byte == _NL:  # ESC+NL = einde record
                if len(message) > 4:
                    yield bytes(message)
                message = bytearray()
                state = _STATE_WAITING
            else:  # onbekende ESC+???-sequentie: negeer dit record, wacht op nieuwe start
                message = bytearray()
                state = _STATE_WAITING
        previous_byte = current_byte


def _parse_can_id(can_id: int) -> Tuple[int, int, int, int]:
    """Ontleedt een 29-bit uitgebreide CAN-ID naar (priority, pgn, source, destination)."""
    source = can_id & 0xFF
    ps = (can_id >> 8) & 0xFF
    pf = (can_id >> 16) & 0xFF
    dp = (can_id >> 24) & 0x1
    priority = (can_id >> 26) & 0x7
    if pf < 240:  # PDU1: gericht bericht, PS-byte is het bestemmingsadres
        pgn = (dp << 16) | (pf << 8)
        destination = ps
    else:  # PDU2: broadcast, PS-byte hoort bij de PGN
        pgn = (dp << 16) | (pf << 8) | ps
        destination = 0xFF
    return priority, pgn, source, destination


def _decode_bst95_record(raw: bytes) -> Optional[Tuple[int, int, int, int, bytes]]:
    """raw = alles ná de '07 95'-header: lengte(1) + tijdteller(2, genegeerd) + CAN-ID(4) + data."""
    if len(raw) < 8:
        return None
    if raw[0] != len(raw) - 1:
        return None  # lengteveld klopt niet -> waarschijnlijk een corrupt record
    can_id = raw[3] | (raw[4] << 8) | (raw[5] << 16) | (raw[6] << 24)
    priority, pgn, source, destination = _parse_can_id(can_id)
    return priority, pgn, source, destination, raw[7:]


@dataclass
class _FastPacketAssembly:
    seq_counter: int
    total_length: int
    data: bytearray
    next_frame_index: int


def _reassemble_fast_packet(
    key: Tuple[int, int], payload: bytes, state: Dict[Tuple[int, int], _FastPacketAssembly]
) -> Optional[bytes]:
    """NMEA2000 Fast Packet-reassemblage: byte 0 = (volgnummer<<5 | frame-index)."""
    if len(payload) < 2:
        return None
    frame_header = payload[0]
    seq_counter = frame_header >> 5
    frame_index = frame_header & 0x1F

    if frame_index == 0:
        total_length = payload[1]
        assembly = _FastPacketAssembly(seq_counter, total_length, bytearray(payload[2:8]), 1)
        state[key] = assembly
    else:
        assembly = state.get(key)
        if assembly is None or assembly.seq_counter != seq_counter or assembly.next_frame_index != frame_index:
            state.pop(key, None)  # gemiste of onverwachte frame -> deze reassemblage opgeven
            return None
        assembly.data.extend(payload[1:8])
        assembly.next_frame_index += 1

    if len(assembly.data) >= assembly.total_length:
        state.pop(key, None)
        return bytes(assembly.data[: assembly.total_length])
    return None


def iter_frames(path: Union[str, Path]) -> Iterator[Frame]:
    """Leest een EBL-logbestand en geeft er gedecodeerde Frame's van terug, in bestandsvolgorde.

    Vereist dat het bestand ergens een PGN 126992 (System Time)-boodschap bevat om een
    absolute tijdreferentie te krijgen; frames daarvóór worden overgeslagen.
    """
    data = Path(path).read_bytes()
    fast_packet_state: Dict[Tuple[int, int], _FastPacketAssembly] = {}
    current_time = None

    for record in _iter_raw_records(data):
        if len(record) < 2 or record[0] != 0x07 or record[1] != _CMD_RAW_ACTISENSE_MESSAGE_RECEIVED:
            continue
        decoded = _decode_bst95_record(record[2:])
        if decoded is None:
            continue
        priority, pgn, source, destination, payload = decoded

        if pgn in _FAST_PACKET_PGNS:
            payload = _reassemble_fast_packet((source, pgn), payload, fast_packet_state)
            if payload is None:
                continue

        if pgn == PGN_SYSTEM_TIME:
            decoded_time = decode_system_time(payload)
            if decoded_time is not None:
                current_time = decoded_time
            continue

        if current_time is None:
            continue  # nog geen tijdreferentie gezien in dit bestand

        yield Frame(
            time=current_time, source=source, destination=destination, priority=priority, pgn=pgn, data=payload
        )
