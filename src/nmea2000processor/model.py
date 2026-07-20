from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import FrozenSet, Optional


@dataclass(frozen=True)
class Frame:
    """Eén NMEA2000-boodschap, al herassembleerd door de Actisense-hardware (N2K ASCII-formaat)."""

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
    sog_ms: float  # snelheid over de grond, meters/seconde


@dataclass(frozen=True)
class EngineSample:
    time: datetime
    instance: int
    fuel_rate_lph: Optional[float]  # brandstofverbruik in L/uur, None = niet beschikbaar
    total_hours_s: Optional[int]  # cumulatieve draaiurenteller van de motor, in seconden
    oil_pressure_pa: Optional[float] = None
    oil_temperature_k: Optional[float] = None
    coolant_temperature_k: Optional[float] = None
    alternator_voltage_v: Optional[float] = None
    engine_load_pct: Optional[float] = None
    warnings: FrozenSet[str] = field(default_factory=frozenset)  # actieve waarschuwingsvlaggen


@dataclass(frozen=True)
class TripFuelSample:
    time: datetime
    instance: int
    trip_fuel_used_l: Optional[float]  # triptmeter van de motor zelf (PGN 127497), in liter


@dataclass(frozen=True)
class DepthSample:
    time: datetime
    depth_m: Optional[float]  # waterdiepte onder de transducer (PGN 128267), in meter
