from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import FrozenSet, Optional

# slots=True on every sample type below (not just Frame, the highest-volume one): a full season's
# worth of decoded samples -- easily millions of GPS fixes/SOG samples across a real multi-year
# archive -- all stay resident in memory for the whole run at once (tripbuilder needs the complete,
# merged history to find trip boundaries), so per-instance overhead multiplies directly into total
# RSS. Found in practice: this is what actually got the Android app OOM-killed by the phone's OS,
# not the on-disk sample cache (see sample_cache.py's own docstring for that separate fix) --
# measured ~38% less memory per instance with slots than the plain dict-backed default, for zero
# behavior change (still frozen/hashable/comparable the same way).


@dataclass(frozen=True, slots=True)
class Frame:
    """A single, fully reassembled NMEA2000 message (Fast Packet frames already joined back
    together, see ebl_reader.py)."""

    time: datetime
    source: int
    destination: int
    priority: int
    pgn: int
    data: bytes


@dataclass(frozen=True, slots=True)
class PositionFix:
    time: datetime
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class SogSample:
    time: datetime
    sog_ms: float  # speed over ground, meters/second
    cog_deg: Optional[float] = None  # course over ground, degrees (PGN 129026, same message as SOG)


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class EngineRpmSample:
    time: datetime
    instance: int
    rpm: Optional[float]  # engine speed in RPM (PGN 127488), None = not available


@dataclass(frozen=True, slots=True)
class TripFuelSample:
    time: datetime
    instance: int
    trip_fuel_used_l: Optional[float]  # the engine's own trip meter (PGN 127497), in liters


@dataclass(frozen=True, slots=True)
class DepthSample:
    time: datetime
    depth_m: Optional[float]  # water depth under the transducer (PGN 128267), in meters


@dataclass(frozen=True, slots=True)
class WaterTempSample:
    time: datetime
    temp_c: Optional[float]  # sea/outside water temperature (PGN 130312), in degrees Celsius


@dataclass(frozen=True, slots=True)
class BatterySample:
    time: datetime
    instance: int
    voltage_v: Optional[float]  # battery monitor's own voltage reading (PGN 127508), in volts


@dataclass(frozen=True, slots=True)
class AttitudeSample:
    time: datetime
    pitch_deg: Optional[float]  # PGN 127257, degrees
    roll_deg: Optional[float]  # PGN 127257, degrees
