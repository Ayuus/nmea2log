"""Decodeert de payload-bytes van een N2K-boodschap voor de PGN's die het logboek nodig heeft.

Veldindeling (bit-offset/lengte/resolutie) is overgenomen uit het publieke, door de community
onderhouden NMEA2000-woordenboek van het canboat-project (https://github.com/canboat/canboat,
docs/canboat.json). Multi-byte velden zijn little-endian; bitvelden worden LSB-eerst gepakt,
zoals gebruikelijk in NMEA2000/J1939.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, FrozenSet, Optional, Tuple

PGN_POSITION_RAPID = 129025  # Position, Rapid Update
PGN_COG_SOG_RAPID = 129026  # COG & SOG, Rapid Update
PGN_ENGINE_DYNAMIC = 127489  # Engine Parameters, Dynamic
PGN_TRIP_FUEL_ENGINE = 127497  # Trip Parameters, Engine
PGN_WATER_DEPTH = 128267  # Water Depth
PGN_SYSTEM_TIME = 126992  # System Time

_EPOCH = date(1970, 1, 1)

# Bitbetekenis van de twee "Discrete Status"-velden in PGN 127489, overgenomen uit canboat's
# ENGINE_STATUS_1 / ENGINE_STATUS_2 lookup-enumeraties.
_ENGINE_STATUS_1_BITS = {
    0: "Check Engine",
    1: "Over Temperature",
    2: "Low Oil Pressure",
    3: "Low Oil Level",
    4: "Low Fuel Pressure",
    5: "Low System Voltage",
    6: "Low Coolant Level",
    7: "Water Flow",
    8: "Water In Fuel",
    9: "Charge Indicator",
    10: "Preheat Indicator",
    11: "High Boost Pressure",
    12: "Rev Limit Exceeded",
    13: "EGR System",
    14: "Throttle Position Sensor",
    15: "Emergency Stop",
}
_ENGINE_STATUS_2_BITS = {
    0: "Warning Level 1",
    1: "Warning Level 2",
    2: "Power Reduction",
    3: "Maintenance Needed",
    4: "Engine Comm Error",
    5: "Sub or Secondary Throttle",
    6: "Neutral Start Protect",
    7: "Engine Shutting Down",
}


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


def _decode_bit_warnings(raw: Optional[int], bit_names: Dict[int, str]) -> FrozenSet[str]:
    if raw is None:
        return frozenset()
    return frozenset(name for bit, name in bit_names.items() if raw & (1 << bit))


def decode_engine_dynamic(data: bytes) -> Optional[dict]:
    """PGN 127489: motor-instance, brandstofverbruik, draaiuren en gezondheidsindicatoren.

    Geeft een dict terug met kwargs die direct in ``EngineSample(time=..., **result)`` passen.
    """
    instance = _extract(data, 0, 8, signed=False)
    if instance is None:
        return None

    oil_pressure_raw = _extract(data, 8, 16, signed=False)
    oil_temperature_raw = _extract(data, 24, 16, signed=False)
    coolant_temperature_raw = _extract(data, 40, 16, signed=False)
    alternator_raw = _extract(data, 56, 16, signed=True)
    fuel_raw = _extract(data, 72, 16, signed=True)
    hours_raw = _extract(data, 88, 32, signed=False)
    status1_raw = _extract(data, 160, 16, signed=False)
    status2_raw = _extract(data, 176, 16, signed=False)
    engine_load_raw = _extract(data, 192, 8, signed=True)

    warnings = _decode_bit_warnings(status1_raw, _ENGINE_STATUS_1_BITS) | _decode_bit_warnings(
        status2_raw, _ENGINE_STATUS_2_BITS
    )

    return {
        "instance": instance,
        "fuel_rate_lph": fuel_raw * 0.1 if fuel_raw is not None else None,
        "total_hours_s": hours_raw,
        "oil_pressure_pa": oil_pressure_raw * 100.0 if oil_pressure_raw is not None else None,
        "oil_temperature_k": oil_temperature_raw * 0.1 if oil_temperature_raw is not None else None,
        "coolant_temperature_k": coolant_temperature_raw * 0.01 if coolant_temperature_raw is not None else None,
        "alternator_voltage_v": alternator_raw * 0.01 if alternator_raw is not None else None,
        "engine_load_pct": float(engine_load_raw) if engine_load_raw is not None else None,
        "warnings": warnings,
    }


def decode_water_depth(data: bytes) -> Optional[float]:
    """PGN 128267: waterdiepte onder de transducer, in meter."""
    depth_raw = _extract(data, 8, 32, signed=False)
    if depth_raw is None:
        return None
    return depth_raw * 0.01


def decode_trip_fuel_engine(data: bytes) -> Optional[Tuple[int, Optional[float]]]:
    """PGN 127497: motor-instance en de triptmeter-brandstofstand van de motor zelf, in liter."""
    instance = _extract(data, 0, 8, signed=False)
    if instance is None:
        return None
    trip_fuel_raw = _extract(data, 8, 16, signed=False)
    trip_fuel_l = float(trip_fuel_raw) if trip_fuel_raw is not None else None
    return instance, trip_fuel_l


def decode_system_time(data: bytes) -> Optional[datetime]:
    """PGN 126992: absolute datum/tijd (UTC) — dagen sinds 1970-01-01 plus tijd-op-de-dag.

    Gebruikt om EBL-logbestanden (die geen bruikbare eigen tijdstempel per record hebben) van
    een absolute klok te voorzien, via de systeemtijd-PGN die elders in dezelfde N2K-stream zit.
    """
    date_raw = _extract(data, 16, 16, signed=False)
    time_raw = _extract(data, 32, 32, signed=False)
    if date_raw is None or time_raw is None:
        return None
    day = _EPOCH + timedelta(days=date_raw)
    return datetime.combine(day, datetime.min.time()) + timedelta(seconds=time_raw * 0.0001)
