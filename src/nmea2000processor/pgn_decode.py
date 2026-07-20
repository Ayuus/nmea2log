"""Decodeert de payload-bytes van een N2K-boodschap voor de PGN's die het logboek nodig heeft.

Veldindeling (bit-offset/lengte/resolutie) is overgenomen uit het publieke, door de community
onderhouden NMEA2000-woordenboek van het canboat-project (https://github.com/canboat/canboat,
docs/canboat.json). Multi-byte velden zijn little-endian; bitvelden worden LSB-eerst gepakt,
zoals gebruikelijk in NMEA2000/J1939.
"""

from __future__ import annotations

from typing import Optional, Tuple

PGN_POSITION_RAPID = 129025  # Position, Rapid Update
PGN_COG_SOG_RAPID = 129026  # COG & SOG, Rapid Update
PGN_ENGINE_DYNAMIC = 127489  # Engine Parameters, Dynamic
PGN_TRIP_FUEL_ENGINE = 127497  # Trip Parameters, Engine


def _extract(data: bytes, bit_offset: int, bit_length: int, *, signed: bool) -> Optional[int]:
    """Lees een little-endian bitveld uit een N2K-payload en herken 'niet beschikbaar'-waarden."""
    byte_start = bit_offset // 8
    bit_shift = bit_offset % 8
    n_bytes = (bit_shift + bit_length + 7) // 8
    chunk = data[byte_start : byte_start + n_bytes]
    if len(chunk) < n_bytes:
        return None

    raw = int.from_bytes(chunk, byteorder="little")
    raw >>= bit_shift
    raw &= (1 << bit_length) - 1

    if signed:
        sign_bit = 1 << (bit_length - 1)
        not_available = sign_bit - 1  # hoogste positieve waarde = "niet beschikbaar" (NMEA2000-conventie)
        if raw == not_available:
            return None
        if raw & sign_bit:
            raw -= 1 << bit_length
    else:
        if raw == (1 << bit_length) - 1:  # alle bits 1 = "niet beschikbaar"
            return None
    return raw


def decode_position_rapid(data: bytes) -> Optional[Tuple[float, float]]:
    """PGN 129025: breedte- en lengtegraad in graden."""
    lat_raw = _extract(data, 0, 32, signed=True)
    lon_raw = _extract(data, 32, 32, signed=True)
    if lat_raw is None or lon_raw is None:
        return None
    return lat_raw * 1e-7, lon_raw * 1e-7


def decode_sog(data: bytes) -> Optional[float]:
    """PGN 129026: snelheid over de grond in m/s."""
    sog_raw = _extract(data, 32, 16, signed=False)
    if sog_raw is None:
        return None
    return sog_raw * 0.01


def decode_engine_dynamic(data: bytes) -> Optional[Tuple[int, Optional[float], Optional[int]]]:
    """PGN 127489: motor-instance, brandstofverbruik (L/uur) en totale draaiuren (seconden)."""
    instance = _extract(data, 0, 8, signed=False)
    if instance is None:
        return None
    fuel_raw = _extract(data, 72, 16, signed=True)
    hours_raw = _extract(data, 88, 32, signed=False)
    fuel_lph = fuel_raw * 0.1 if fuel_raw is not None else None
    return instance, fuel_lph, hours_raw


def decode_trip_fuel_engine(data: bytes) -> Optional[Tuple[int, Optional[float]]]:
    """PGN 127497: motor-instance en de triptmeter-brandstofstand van de motor zelf, in liter."""
    instance = _extract(data, 0, 8, signed=False)
    if instance is None:
        return None
    trip_fuel_raw = _extract(data, 8, 16, signed=False)
    trip_fuel_l = float(trip_fuel_raw) if trip_fuel_raw is not None else None
    return instance, trip_fuel_l
