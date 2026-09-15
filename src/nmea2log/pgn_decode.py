"""Decodes the payload bytes of an N2K message for the PGNs the logbook needs.

Field layout (bit offset/length/resolution) is taken from the public, community-maintained
NMEA2000 dictionary of the canboat project (https://github.com/canboat/canboat,
docs/canboat.json). Multi-byte fields are little-endian; bit fields are packed LSB-first, as is
customary in NMEA2000/J1939.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Dict, FrozenSet, Optional, Tuple

PGN_POSITION_RAPID = 129025  # Position, Rapid Update
PGN_COG_SOG_RAPID = 129026  # COG & SOG, Rapid Update
PGN_ENGINE_DYNAMIC = 127489  # Engine Parameters, Dynamic
PGN_ENGINE_RAPID = 127488  # Engine Parameters, Rapid Update
PGN_TRIP_FUEL_ENGINE = 127497  # Trip Parameters, Engine
PGN_WATER_DEPTH = 128267  # Water Depth
PGN_SYSTEM_TIME = 126992  # System Time
PGN_TEMPERATURE = 130312  # Temperature
PGN_BATTERY_STATUS = 127508  # Battery Status
PGN_ATTITUDE = 127257  # Attitude (pitch/roll/yaw)

_EPOCH = date(1970, 1, 1)
_KELVIN_TO_CELSIUS = 273.15

# canboat's TEMPERATURE_SOURCE lookup enumeration for PGN 130312's "Source" field; 0 is the one
# we want (sea/outside water temperature, as opposed to e.g. cabin or exhaust gas temperature).
_TEMPERATURE_SOURCE_SEA = 0

# Bit meanings of the two "Discrete Status" fields in PGN 127489, taken from canboat's
# ENGINE_STATUS_1 / ENGINE_STATUS_2 lookup enumerations.
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
    """Read a little-endian bit field from an N2K payload and recognize 'not available' values."""
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
        not_available = sign_bit - 1  # highest positive value = "not available" (NMEA2000 convention)
        if raw == not_available:
            return None
        if raw & sign_bit:
            raw -= 1 << bit_length
    else:
        if raw == (1 << bit_length) - 1:  # all bits 1 = "not available"
            return None
    return raw


def decode_position_rapid(data: bytes) -> Optional[Tuple[float, float]]:
    """PGN 129025: latitude and longitude in degrees."""
    lat_raw = _extract(data, 0, 32, signed=True)
    lon_raw = _extract(data, 32, 32, signed=True)
    if lat_raw is None or lon_raw is None:
        return None
    return lat_raw * 1e-7, lon_raw * 1e-7


def decode_sog(data: bytes) -> Optional[float]:
    """PGN 129026: speed over ground in m/s."""
    sog_raw = _extract(data, 32, 16, signed=False)
    if sog_raw is None:
        return None
    return sog_raw * 0.01


def decode_cog(data: bytes) -> Optional[float]:
    """PGN 129026: course over ground in degrees (0-360), true or magnetic depending on the
    "COG Reference" field -- not decoded separately here since the W2K-2/GPS combination this app
    was built for always reports true."""
    cog_raw = _extract(data, 16, 16, signed=False)
    if cog_raw is None:
        return None
    return math.degrees(cog_raw * 0.0001)


def _decode_bit_warnings(raw: Optional[int], bit_names: Dict[int, str]) -> FrozenSet[str]:
    if raw is None:
        return frozenset()
    return frozenset(name for bit, name in bit_names.items() if raw & (1 << bit))


def decode_engine_rapid(data: bytes) -> Optional[Tuple[int, Optional[float]]]:
    """PGN 127488: engine instance and engine speed (RPM). Sent much more frequently than PGN
    127489's other engine fields, so it's decoded as its own sample stream."""
    instance = _extract(data, 0, 8, signed=False)
    if instance is None:
        return None
    rpm_raw = _extract(data, 8, 16, signed=False)
    rpm = rpm_raw * 0.25 if rpm_raw is not None else None
    return instance, rpm


def decode_engine_dynamic(data: bytes) -> Optional[dict]:
    """PGN 127489: engine instance, fuel consumption, engine hours, and health indicators.

    Returns a dict with kwargs that plug directly into ``EngineSample(time=..., **result)``.
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
    """PGN 128267: water depth under the transducer, in meters."""
    depth_raw = _extract(data, 8, 32, signed=False)
    if depth_raw is None:
        return None
    return depth_raw * 0.01


def decode_sea_temperature(data: bytes) -> Optional[float]:
    """PGN 130312: water temperature under the hull, in degrees Celsius. Several sources can be
    reported under this PGN (cabin, exhaust gas, ...); returns None unless the "Source" field is
    specifically sea/outside water temperature (see ``_TEMPERATURE_SOURCE_SEA``)."""
    source = _extract(data, 16, 8, signed=False)
    if source != _TEMPERATURE_SOURCE_SEA:
        return None
    temp_raw = _extract(data, 24, 16, signed=False)
    if temp_raw is None:
        return None
    return temp_raw * 0.01 - _KELVIN_TO_CELSIUS


def decode_battery_status(data: bytes) -> Optional[Tuple[int, Optional[float]]]:
    """PGN 127508: battery instance and voltage, in volts. Distinct from PGN 127489's alternator
    voltage -- that's the engine's charging output, only present while the engine PGN is being
    sent (effectively only while running); this is a dedicated battery monitor's own reading,
    which keeps reporting at anchor with the engine off too."""
    instance = _extract(data, 0, 8, signed=False)
    if instance is None:
        return None
    voltage_raw = _extract(data, 8, 16, signed=True)
    voltage_v = voltage_raw * 0.01 if voltage_raw is not None else None
    return instance, voltage_v


def decode_attitude(data: bytes) -> Optional[Tuple[Optional[float], Optional[float]]]:
    """PGN 127257: pitch and roll, in degrees (yaw is decoded by nothing here, not needed).
    Returns None only if neither is available."""
    pitch_raw = _extract(data, 24, 16, signed=True)
    roll_raw = _extract(data, 40, 16, signed=True)
    if pitch_raw is None and roll_raw is None:
        return None
    pitch_deg = math.degrees(pitch_raw * 0.0001) if pitch_raw is not None else None
    roll_deg = math.degrees(roll_raw * 0.0001) if roll_raw is not None else None
    return pitch_deg, roll_deg


def decode_trip_fuel_engine(data: bytes) -> Optional[Tuple[int, Optional[float]]]:
    """PGN 127497: engine instance and the engine's own trip-meter fuel reading, in liters."""
    instance = _extract(data, 0, 8, signed=False)
    if instance is None:
        return None
    trip_fuel_raw = _extract(data, 8, 16, signed=False)
    trip_fuel_l = float(trip_fuel_raw) if trip_fuel_raw is not None else None
    return instance, trip_fuel_l


def decode_system_time(data: bytes) -> Optional[datetime]:
    """PGN 126992: absolute date/time (UTC) -- days since 1970-01-01 plus time of day.

    Used to give EBL log files (which have no usable timestamp of their own per record) an
    absolute clock, via the system-time PGN found elsewhere in the same N2K stream.
    """
    date_raw = _extract(data, 16, 16, signed=False)
    time_raw = _extract(data, 32, 32, signed=False)
    if date_raw is None or time_raw is None:
        return None
    day = _EPOCH + timedelta(days=date_raw)
    return datetime.combine(day, datetime.min.time()) + timedelta(seconds=time_raw * 0.0001)
