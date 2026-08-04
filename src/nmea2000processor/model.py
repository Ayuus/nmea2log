from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import FrozenSet, Optional


@dataclass(frozen=True)
class Frame:
    """A single NMEA2000 message, already reassembled by the Actisense hardware (N2K ASCII format)."""

    time: datetime
    source: int
    destination: int
    priority: int
    pgn: int
    data: bytes


@dataclass(frozen=True)
class PositionFix:
    time: datetime
    lat: float
    lon: float


@dataclass(frozen=True)
class SogSample:
    time: datetime
    sog_ms: float  # speed over ground, meters/second


@dataclass(frozen=True)
class EngineSample:
    time: datetime
    instance: int
    fuel_rate_lph: Optional[float]  # fuel consumption in L/hour, None = not available
    total_hours_s: Optional[int]  # engine's cumulative hour meter, in seconds
    oil_pressure_pa: Optional[float] = None
    oil_temperature_k: Optional[float] = None
    coolant_temperature_k: Optional[float] = None
    alternator_voltage_v: Optional[float] = None
    engine_load_pct: Optional[float] = None
    warnings: FrozenSet[str] = field(default_factory=frozenset)  # active warning flags


@dataclass(frozen=True)
class TripFuelSample:
    time: datetime
    instance: int
    trip_fuel_used_l: Optional[float]  # the engine's own trip meter (PGN 127497), in liters


@dataclass(frozen=True)
class DepthSample:
    time: datetime
    depth_m: Optional[float]  # water depth under the transducer (PGN 128267), in meters


@dataclass(frozen=True)
class WaterTempSample:
    time: datetime
    temp_c: Optional[float]  # sea/outside water temperature (PGN 130312), in degrees Celsius


@dataclass(frozen=True)
class BatterySample:
    time: datetime
    instance: int
    voltage_v: Optional[float]  # battery monitor's own voltage reading (PGN 127508), in volts
