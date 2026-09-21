"""Columnar, array.array-backed storage for a season's worth of PositionFix/SogSample -- built to
replace the plain Python lists pipeline.py used to accumulate across an entire
archive (2000+ files) before ever reaching tripbuilder.py.

Why this exists: a real multi-year archive holds millions of GPS fixes, and each stays resident
for the *whole* run (tripbuilder needs the complete, time-ordered history to find trip
boundaries) -- on a memory-constrained phone this is what actually got the Android app OOM-killed
by the OS (see model.py's own slots=True note for the first, smaller pass at this same problem).
A Python object -- even a frozen, slots dataclass -- still boxes every one of its float/datetime
fields as its own separate heap object; a plain array.array of doubles stores raw C values with no
per-element object overhead at all. Measured on a representative 200k-sample set: ~152 bytes per
PositionFix(slots) instance vs. ~25 bytes per sample stored this way (time+lat+lon combined) --
an ~83% reduction, with zero new dependencies (numpy has no prebuilt wheel for this project's
Chaquopy/Android Python version at the time this was written; array.array is pure stdlib and
behaves identically on desktop and Android).

Time is stored as float seconds since a fixed, timezone-naive epoch (1970-01-01) -- NOT via
datetime.timestamp(), which reinterprets a naive datetime as *local* time and would silently
produce a different value depending on the machine's timezone. Every timestamp elsewhere in this
codebase is a naive UTC value; encoding/decoding here must round-trip it exactly as given.
"""

from __future__ import annotations

import array
import bisect
import itertools
import math
from datetime import datetime, timedelta
from typing import Dict, FrozenSet, Iterable, Iterator, List, Optional, Tuple, Union

from .log import log
from .model import (
    AttitudeSample,
    BatterySample,
    DepthSample,
    EngineRpmSample,
    EngineSample,
    PositionFix,
    SogSample,
    TripFuelSample,
    WaterTempSample,
)

_EPOCH = datetime(1970, 1, 1)

# A backward jump at least this large means a device clock that hadn't synced to real time yet,
# not ordinary jitter/duplicate timestamps (found in practice: real jumps were on the order of
# weeks; real jitter/duplicates are seconds at most). Shared with tripbuilder.py's own outlier
# rejection -- see SogArray/AttitudeArray.drop_time_regressions and _reject_gps_outliers_array.
CLOCK_JUMP_THRESHOLD_S = 24 * 3600.0


def to_epoch(dt: datetime) -> float:
    return (dt - _EPOCH).total_seconds()


def from_epoch(seconds: float) -> datetime:
    return _EPOCH + timedelta(seconds=seconds)


def _is_sorted(time_column: "array.array[float]") -> bool:
    """Cheap O(n)-time, O(1)-extra-memory check for whether a time column is already in
    non-decreasing order -- used to skip drop_time_regressions()'s own sort+copy entirely when it would
    be a no-op. Confirmed, in practice, to matter: sorted(range(n), key=...) (the actual sort,
    when needed) has to materialize a full list of boxed index *and* key objects for the whole
    array to sort it at all -- on a real ~2 million-row season-wide array, this was found (via
    fine-grained checkpoint logging on a real device) to be exactly where a full-archive rebuild
    was getting OOM-killed. A season's data is typically already in (or very close to) time order
    to begin with (each source file's own records are chronological; only the *file discovery*
    order can differ from that -- see AttitudeArray/SogArray's own drop_time_regressions docstrings) --
    this plain linear scan, using array.array's own fast C-level indexing, costs only a handful of
    float comparisons per element and never boxes anything beyond what a single comparison needs
    at a time, regardless of how large the array is."""
    return all(time_column[i] <= time_column[i + 1] for i in range(len(time_column) - 1))


def log_time_anomaly(label: str, dropped: int, max_backward_s: float, first_epoch: Optional[float], clock_resets: int) -> None:
    """Called whenever a time column turned out *not* to be in order -- which, with the decoder
    picking one consistent System Time source (see ebl_reader.py), should never happen on real
    data any more. Loud on purpose (its own ``[anomaly]`` tag, not ``[warning]``: the Android app
    treats every ``[warning]`` line as a lost-connection notice): silently repairing this is
    exactly what hid the real cause of the earlier OOM crashes (a second device broadcasting a
    wrong clock), so an unexpected reappearance should be visible, not swallowed."""
    parts = []
    if dropped:
        parts.append(
            f"dropped {dropped} row(s) that went backward in time (largest jump {max_backward_s:.1f}s"
            + (f", first at {from_epoch(first_epoch)}" if first_epoch is not None else "")
            + ")"
        )
    if clock_resets:
        parts.append(f"accepted {clock_resets} large backward jump(s) as a clock reset")
    log(
        f"[anomaly] {label}: " + "; ".join(parts) + " -- the source data is not in time order. "
        "This should not happen and needs investigating (a second device broadcasting a "
        "different clock was the cause last time); the affected rows were repaired, not trusted."
    )


class _SampleArray:
    """Shared, non-hot-path plumbing for the array.array-backed collections below -- every one of
    them keeps a ``_time`` column (float seconds, see the module docstring) alongside its own
    value columns, and behaves like a plain list of its sample type for equality/repr/len.
    Deliberately *not* also sharing ``append``/``__iter__``: those run once per sample (tens of
    millions of times for a real season's attitude data), so each subclass keeps its own,
    straight-line version instead of a generic, per-column dispatch loop."""

    __slots__ = ()

    _time: "array.array[float]"

    def extend(self, samples: Iterable) -> None:
        for sample in samples:
            self.append(sample)

    def time_at(self, i: int) -> float:
        return self._time[i]

    def __len__(self) -> int:
        return len(self._time)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (type(self), list)):
            return list(self) == list(other)
        return NotImplemented

    def __repr__(self) -> str:
        return f"{type(self).__name__}({list(self)!r})"


class _InstancedSampleArray(_SampleArray):
    """A _SampleArray whose samples carry an ``instance`` (engine/battery number) -- supports
    indexed access, so callers can look at a single row (or a small window of rows) without
    materializing the season's whole worth of sample objects (see tripbuilder.py's
    _InstanceIndex). Iteration is built on that same indexed access."""

    __slots__ = ()

    _instance: "array.array[float]"

    def instance_at(self, i: int) -> int:
        return int(self._instance[i])

    def __getitem__(self, i: int):
        raise NotImplementedError

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]


class _SortableSampleArray(_SampleArray):
    """A _SampleArray that build_trips() needs in time order (speed and attitude samples)."""

    __slots__ = ()

    _LABEL = "samples"  # what the [anomaly] line calls this collection; overridden per subclass

    def drop_time_regressions(self):
        """Enforces non-decreasing time (despite what an earlier name of this suggested, it never
        sorts) -- the data should already be in order, so anything it has to drop or accept is
        reported via log_time_anomaly. Array-native equivalent of what
        sorted(samples, key=lambda s: s.time) would otherwise have needed -- used by
        build_trips()/_merge_nav_samples() (see tripbuilder.py) so a season's worth of these
        samples (SOG: similar cardinality to position fixes, millions on a real multi-year
        archive; attitude: ~37 million rows, more than 13x the GPS fix count) never has to be
        materialized into a plain list of sample objects just to sort it, the same problem
        FixArray's own outlier-rejection pass already solved for position fixes.

        Mutates and returns this same object (rather than building and returning an unrelated new
        one) for the same reason FixArray.replace_columns_with exists: a caller that keeps its own
        reference to this exact object across build_trips()'s whole run (every real caller does,
        see pipeline.py) would otherwise also keep the original, unsorted columns
        resident the entire time, alongside this sorted copy, for no reason.

        A no-op when already sorted -- see _is_sorted's own docstring. Otherwise, drops whichever
        rows cause a small backward jump rather than sorting them into place -- confirmed, on a
        real archive, that the backward jumps actually seen here aren't file-ordering noise but a
        device logging with a stale/uncorrected clock for a few readings before its first
        accurate PGN 126992 (System Time) update (every frame's own timestamp is simply whichever
        System Time reading was last seen -- see ebl_reader.py's own docstring), off by whole
        weeks in the cases actually seen; reordering a wrong reading into a different position
        wouldn't have made it a right one. A single forward pass -- keep a row only if its time
        doesn't go backward relative to the last *kept* row -- both fixes that and is exactly why
        the result ends up sorted without ever needing sorted(range(n), key=...)'s own full list
        of boxed index *and* key objects (confirmed, via fine-grained checkpoint logging on a real
        device, to be where a full-archive rebuild was actually getting OOM-killed).

        A jump of at least CLOCK_JUMP_THRESHOLD_S is trusted and reset onto, not dropped --
        confirmed in practice: an earlier version that dropped every backward-going row
        regardless of size got permanently anchored on the wrong, weeks-in-the-future row once
        one of these clock-sync jumps happened, then kept dropping every genuinely good,
        correctly-timed row after it (they all looked "earlier" than that wrong anchor) until
        real time caught all the way back up to it -- on a real archive with such a jump near
        its start, that silently threw away nearly the entire season."""
        if _is_sorted(self._time):
            return self
        keep = bytearray(len(self._time))
        last_time = None
        dropped = clock_resets = 0
        max_backward_s = 0.0
        first_dropped_at: Optional[float] = None
        for i, t in enumerate(self._time):
            if last_time is not None and t < last_time:
                if last_time - t < CLOCK_JUMP_THRESHOLD_S:
                    dropped += 1  # small backward jitter/duplicate -- drop it
                    max_backward_s = max(max_backward_s, last_time - t)
                    if first_dropped_at is None:
                        first_dropped_at = t
                    continue
                clock_resets += 1
            keep[i] = 1
            last_time = t
        log_time_anomaly(self._LABEL, dropped, max_backward_s, first_dropped_at, clock_resets)
        for column_name in self.__slots__:
            column = getattr(self, column_name)
            setattr(self, column_name, array.array(column.typecode, itertools.compress(column, keep)))
        return self


class FixArray(_SampleArray):
    """Ordered collection of PositionFix values, stored as three parallel array.array('d')
    columns (time, lat, lon) instead of one object per fix. Supports enough of a plain
    List[PositionFix]'s interface (len, iteration, equality against a list) to be a drop-in
    replacement anywhere a season's worth of fixes gets accumulated or handed to build_trips()."""

    __slots__ = ("_time", "_lat", "_lon")

    def __init__(self, fixes: Iterable[PositionFix] = ()) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._lat: "array.array[float]" = array.array("d")
        self._lon: "array.array[float]" = array.array("d")
        self.extend(fixes)

    def append(self, fix: PositionFix) -> None:
        self.append_raw(to_epoch(fix.time), fix.lat, fix.lon)

    def append_raw(self, epoch_seconds: float, lat: float, lon: float) -> None:
        """Appends an already-epoch-encoded row directly -- used internally (see
        tripbuilder.py's _reject_gps_outliers_array) to build a filtered/reordered FixArray
        without ever reconstructing a PositionFix object in between."""
        self._time.append(epoch_seconds)
        self._lat.append(lat)
        self._lon.append(lon)

    def lat_at(self, i: int) -> float:
        return self._lat[i]

    def lon_at(self, i: int) -> float:
        return self._lon[i]

    def datetime_at(self, i: int) -> datetime:
        return from_epoch(self._time[i])

    def replace_columns_with(self, other: "FixArray") -> None:
        """Swaps this array's own columns for another FixArray's, in place -- used by
        tripbuilder.py's _reject_gps_outliers_array() so a season-wide FixArray a caller already
        holds a reference to (e.g. android_entry.py's all_fixes, alive for build_trips()'s
        *entire* run) gets its outlier-rejected/sorted data written back into the very same
        object, instead of a second, separate FixArray staying resident alongside the original
        for no reason -- the caller's own reference means the *original*, larger columns would
        otherwise never actually be freed until the whole call returns, even once nothing inside
        build_trips() itself still needs them (confirmed in practice, via a live RSS trace on a
        real ~2.7M-position archive: this was a real, measurable share of build_trips()'s own
        peak). ``other`` is left empty (0 rows) -- not meant to be used again afterward."""
        self._time, self._lat, self._lon = other._time, other._lat, other._lon
        other._time, other._lat, other._lon = array.array("d"), array.array("d"), array.array("d")

    def __getitem__(self, i: int) -> PositionFix:
        # array.array already supports negative indices natively -- e.g. all_fixes[-1] for "the
        # most recent fix in the whole dataset" (see pipeline.py).
        return PositionFix(from_epoch(self._time[i]), self._lat[i], self._lon[i])

    def __iter__(self) -> Iterator[PositionFix]:
        for t, lat, lon in zip(self._time, self._lat, self._lon):
            yield PositionFix(from_epoch(t), lat, lon)


class SogArray(_SortableSampleArray):
    """Same idea as FixArray, for SogSample. cog_deg is Optional in SogSample -- stored as NaN
    when absent, since it's already a float field and NaN never legitimately occurs otherwise."""

    __slots__ = ("_time", "_sog_ms", "_cog_deg")
    _LABEL = "Speed (SOG) samples"

    def __init__(self, sogs: Iterable[SogSample] = ()) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._sog_ms: "array.array[float]" = array.array("d")
        self._cog_deg: "array.array[float]" = array.array("d")
        self.extend(sogs)

    def append(self, sog: SogSample) -> None:
        self._time.append(to_epoch(sog.time))
        self._sog_ms.append(sog.sog_ms)
        self._cog_deg.append(sog.cog_deg if sog.cog_deg is not None else math.nan)

    def sog_at(self, i: int) -> float:
        return self._sog_ms[i]

    def cog_at(self, i: int) -> Optional[float]:
        cog = self._cog_deg[i]
        return None if math.isnan(cog) else cog

    def __iter__(self) -> Iterator[SogSample]:
        for t, sog_ms, cog in zip(self._time, self._sog_ms, self._cog_deg):
            yield SogSample(from_epoch(t), sog_ms, None if math.isnan(cog) else cog)


class DepthArray(_SortableSampleArray):
    """Same idea as FixArray, for DepthSample. depth_m is Optional -- stored as NaN when absent."""

    __slots__ = ("_time", "_depth_m")
    _LABEL = "Depth samples"

    def __init__(self, samples: Iterable[DepthSample] = ()) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._depth_m: "array.array[float]" = array.array("d")
        self.extend(samples)

    def append(self, sample: DepthSample) -> None:
        self._time.append(to_epoch(sample.time))
        self._depth_m.append(sample.depth_m if sample.depth_m is not None else math.nan)

    def depth_at(self, i: int) -> Optional[float]:
        depth_m = self._depth_m[i]
        return None if math.isnan(depth_m) else depth_m

    def __iter__(self) -> Iterator[DepthSample]:
        for t, depth_m in zip(self._time, self._depth_m):
            yield DepthSample(from_epoch(t), None if math.isnan(depth_m) else depth_m)


class WaterTempArray(_SortableSampleArray):
    """Same idea as FixArray, for WaterTempSample. temp_c is Optional -- stored as NaN when
    absent."""

    __slots__ = ("_time", "_temp_c")
    _LABEL = "Water temperature samples"

    def __init__(self, samples: Iterable[WaterTempSample] = ()) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._temp_c: "array.array[float]" = array.array("d")
        self.extend(samples)

    def append(self, sample: WaterTempSample) -> None:
        self._time.append(to_epoch(sample.time))
        self._temp_c.append(sample.temp_c if sample.temp_c is not None else math.nan)

    def temp_at(self, i: int) -> Optional[float]:
        temp_c = self._temp_c[i]
        return None if math.isnan(temp_c) else temp_c

    def __iter__(self) -> Iterator[WaterTempSample]:
        for t, temp_c in zip(self._time, self._temp_c):
            yield WaterTempSample(from_epoch(t), None if math.isnan(temp_c) else temp_c)


class BatteryArray(_InstancedSampleArray):
    """Same idea as FixArray, for BatterySample. instance is stored as a float column too (array
    only has one element type per column) -- always a small non-negative integer in practice, so
    the round-trip through float is exact."""

    __slots__ = ("_time", "_instance", "_voltage_v")

    def __init__(self, samples: Iterable[BatterySample] = ()) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._instance: "array.array[float]" = array.array("d")
        self._voltage_v: "array.array[float]" = array.array("d")
        self.extend(samples)

    def append(self, sample: BatterySample) -> None:
        self._time.append(to_epoch(sample.time))
        self._instance.append(sample.instance)
        self._voltage_v.append(sample.voltage_v if sample.voltage_v is not None else math.nan)

    def __getitem__(self, i: int) -> BatterySample:
        voltage_v = self._voltage_v[i]
        return BatterySample(
            from_epoch(self._time[i]), int(self._instance[i]), None if math.isnan(voltage_v) else voltage_v
        )


class RpmArray(_InstancedSampleArray):
    """Same idea as FixArray, for EngineRpmSample (PGN 127488, engine speed) -- on a real
    full-season archive this is comparable in cardinality to GPS fixes (an NMEA2000 engine
    typically reports RPM at least once a second whenever running: confirmed in practice on a
    real ~2326-file archive, roughly 1.75 million samples), so the same columnar approach
    applies here as everywhere else a season-wide accumulator exists. build_trips() only ever
    consumes this via a single filtering pass per trip (see _rpm_at_time/_typical_rpm/
    _typical_rpm_speed_range in tripbuilder.py), so -- unlike NavSampleArray -- a plain
    __iter__ yielding real EngineRpmSample objects one at a time is enough: those functions
    never need to change at all."""

    __slots__ = ("_time", "_instance", "_rpm")

    def __init__(self, samples: Iterable[EngineRpmSample] = ()) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._instance: "array.array[float]" = array.array("d")
        self._rpm: "array.array[float]" = array.array("d")
        self.extend(samples)

    def append(self, sample: EngineRpmSample) -> None:
        self._time.append(to_epoch(sample.time))
        self._instance.append(sample.instance)
        self._rpm.append(sample.rpm if sample.rpm is not None else math.nan)

    def __getitem__(self, i: int) -> EngineRpmSample:
        rpm = self._rpm[i]
        return EngineRpmSample(from_epoch(self._time[i]), int(self._instance[i]), None if math.isnan(rpm) else rpm)


class TripFuelArray(_InstancedSampleArray):
    """Same idea as FixArray, for TripFuelSample (PGN 127497, the engine's own trip fuel meter)."""

    __slots__ = ("_time", "_instance", "_trip_fuel_used_l")

    def __init__(self, samples: Iterable[TripFuelSample] = ()) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._instance: "array.array[float]" = array.array("d")
        self._trip_fuel_used_l: "array.array[float]" = array.array("d")
        self.extend(samples)

    def append(self, sample: TripFuelSample) -> None:
        self._time.append(to_epoch(sample.time))
        self._instance.append(sample.instance)
        self._trip_fuel_used_l.append(
            sample.trip_fuel_used_l if sample.trip_fuel_used_l is not None else math.nan
        )

    def __getitem__(self, i: int) -> TripFuelSample:
        trip_fuel = self._trip_fuel_used_l[i]
        return TripFuelSample(
            from_epoch(self._time[i]), int(self._instance[i]), None if math.isnan(trip_fuel) else trip_fuel
        )


class EngineArray(_InstancedSampleArray):
    """Same idea as FixArray, for EngineSample -- the season-wide list of raw engine PGN
    readings (fuel rate, hour meter, oil/coolant temperature, alternator voltage, engine load)
    build_trips() holds resident for its entire run (every per-trip engine statistics function
    -- _engine_health, _fuel_liters, _engine_hours_delta, ... -- filters this whole list down to
    its own trip's time window, once per trip). On a real full-season archive this is hundreds
    of thousands of samples (confirmed in practice: ~400k on a real 2326-file archive), so the
    same columnar approach applies.

    Only ``warnings`` (a FrozenSet[str], almost always empty in practice) can't go in an
    array.array column -- stored in a plain sparse dict instead (row index -> non-empty warning
    set), so the overwhelmingly common empty case costs nothing, rather than a per-row frozenset
    object regardless."""

    __slots__ = (
        "_time",
        "_instance",
        "_fuel_rate_lph",
        "_total_hours_s",
        "_oil_pressure_pa",
        "_oil_temperature_k",
        "_coolant_temperature_k",
        "_alternator_voltage_v",
        "_engine_load_pct",
        "_warnings",
    )

    def __init__(self, samples: Iterable[EngineSample] = ()) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._instance: "array.array[float]" = array.array("d")
        self._fuel_rate_lph: "array.array[float]" = array.array("d")
        self._total_hours_s: "array.array[float]" = array.array("d")
        self._oil_pressure_pa: "array.array[float]" = array.array("d")
        self._oil_temperature_k: "array.array[float]" = array.array("d")
        self._coolant_temperature_k: "array.array[float]" = array.array("d")
        self._alternator_voltage_v: "array.array[float]" = array.array("d")
        self._engine_load_pct: "array.array[float]" = array.array("d")
        self._warnings: Dict[int, FrozenSet[str]] = {}
        self.extend(samples)

    def append(self, sample: EngineSample) -> None:
        i = len(self._time)
        self._time.append(to_epoch(sample.time))
        self._instance.append(sample.instance)
        self._fuel_rate_lph.append(sample.fuel_rate_lph if sample.fuel_rate_lph is not None else math.nan)
        self._total_hours_s.append(sample.total_hours_s if sample.total_hours_s is not None else math.nan)
        self._oil_pressure_pa.append(sample.oil_pressure_pa if sample.oil_pressure_pa is not None else math.nan)
        self._oil_temperature_k.append(
            sample.oil_temperature_k if sample.oil_temperature_k is not None else math.nan
        )
        self._coolant_temperature_k.append(
            sample.coolant_temperature_k if sample.coolant_temperature_k is not None else math.nan
        )
        self._alternator_voltage_v.append(
            sample.alternator_voltage_v if sample.alternator_voltage_v is not None else math.nan
        )
        self._engine_load_pct.append(sample.engine_load_pct if sample.engine_load_pct is not None else math.nan)
        if sample.warnings:
            self._warnings[i] = sample.warnings

    def __getitem__(self, i: int) -> EngineSample:
        fuel_rate = self._fuel_rate_lph[i]
        total_hours = self._total_hours_s[i]
        oil_pressure = self._oil_pressure_pa[i]
        oil_temperature = self._oil_temperature_k[i]
        coolant_temperature = self._coolant_temperature_k[i]
        alternator_voltage = self._alternator_voltage_v[i]
        engine_load = self._engine_load_pct[i]
        return EngineSample(
            time=from_epoch(self._time[i]),
            instance=int(self._instance[i]),
            fuel_rate_lph=None if math.isnan(fuel_rate) else fuel_rate,
            total_hours_s=None if math.isnan(total_hours) else int(total_hours),
            oil_pressure_pa=None if math.isnan(oil_pressure) else oil_pressure,
            oil_temperature_k=None if math.isnan(oil_temperature) else oil_temperature,
            coolant_temperature_k=None if math.isnan(coolant_temperature) else coolant_temperature,
            alternator_voltage_v=None if math.isnan(alternator_voltage) else alternator_voltage,
            engine_load_pct=None if math.isnan(engine_load) else engine_load,
            warnings=self._warnings.get(i, frozenset()),
        )


class AttitudeArray(_SortableSampleArray):
    """Same idea as FixArray, for AttitudeSample. pitch_deg/roll_deg are Optional -- stored as
    NaN when absent.

    Pitch and roll are single-precision ('f') columns, not doubles like everything else: a real
    season has ~37 million of these rows (more than 13x the GPS fixes), so this is the biggest
    single block of memory in a full decode, and the values don't need the precision -- they come
    off the bus as a 16-bit number in steps of 0.0001 rad (~0.0057 deg), where a float32 is off by
    ~1e-6 deg at most. Only the time column has to stay a double (a float32 can't hold an epoch
    to the second).

    Also supports integer AND slice indexing (unlike the other array types above) -- unlike
    depth/water_temp/battery, which just get iterated in full, _motion_variation() in
    tripbuilder.py narrows this one down to a single trip's own time window with
    bisect.bisect_left/right (needs random-access indexing) followed by a slice, once per trip,
    since a real season can have millions of attitude samples and re-scanning all of them per
    trip was real, measured cost (see that function's own docstring)."""

    __slots__ = ("_time", "_pitch_deg", "_roll_deg")
    _LABEL = "Attitude samples"

    def __init__(self, samples: Iterable[AttitudeSample] = ()) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._pitch_deg: "array.array[float]" = array.array("f")
        self._roll_deg: "array.array[float]" = array.array("f")
        self.extend(samples)

    def append(self, sample: AttitudeSample) -> None:
        self._time.append(to_epoch(sample.time))
        self._pitch_deg.append(sample.pitch_deg if sample.pitch_deg is not None else math.nan)
        self._roll_deg.append(sample.roll_deg if sample.roll_deg is not None else math.nan)

    def __getitem__(self, key: Union[int, slice]) -> Union[AttitudeSample, "AttitudeArray"]:
        if isinstance(key, slice):
            sliced = AttitudeArray()
            sliced._time = self._time[key]
            sliced._pitch_deg = self._pitch_deg[key]
            sliced._roll_deg = self._roll_deg[key]
            return sliced
        pitch = self._pitch_deg[key]
        roll = self._roll_deg[key]
        return AttitudeSample(
            from_epoch(self._time[key]),
            None if math.isnan(pitch) else pitch,
            None if math.isnan(roll) else roll,
        )

    def __iter__(self) -> Iterator[AttitudeSample]:
        for t, pitch, roll in zip(self._time, self._pitch_deg, self._roll_deg):
            yield AttitudeSample(
                from_epoch(t), None if math.isnan(pitch) else pitch, None if math.isnan(roll) else roll
            )


class NavSampleArray:
    """Columnar storage for tripbuilder.py's own merged nav-sample stream (time + position + speed
    + depth + water-temp + course, one row per accepted GPS fix) -- the single biggest per-season
    Python-object cost left once fixes/sogs themselves are already stored this way (see this
    module's own docstring). build_trips() never keeps more than one of these resident at a time,
    but that one instance spans the *entire* archive being processed, for as long as its whole run
    classification/merging pipeline takes -- exactly FixArray's own reasoning, just one layer
    downstream (this is what a real ~2.7M-position archive's build_trips() call was found, in
    practice, to hold as its single biggest cost -- millions of individually-boxed NavSample
    objects, each alive for the whole run).

    Deliberately does NOT expose a NavSample-returning __getitem__/__iter__ the way FixArray does
    for PositionFix -- NavSample lives in tripbuilder.py, which already imports this module (so
    this module importing it back would be circular), and more importantly tripbuilder.py's own
    run-classification/merging pipeline is built specifically to avoid ever materializing a full
    NavSample per row while operating on a season's worth of them (see that module's own
    Group/_group_index_pairs/_materialize helpers) -- only a single trip's or stay's own, already-
    small selection of rows ever gets turned back into real NavSample objects, right before the
    per-trip statistics functions that need real attribute access."""

    __slots__ = ("_time", "_lat", "_lon", "_sog_ms", "_depth_m", "_water_temp_c", "_cog_deg")

    def __init__(self) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._lat: "array.array[float]" = array.array("d")
        self._lon: "array.array[float]" = array.array("d")
        self._sog_ms: "array.array[float]" = array.array("d")
        self._depth_m: "array.array[float]" = array.array("d")
        self._water_temp_c: "array.array[float]" = array.array("d")
        self._cog_deg: "array.array[float]" = array.array("d")

    def append_raw(
        self,
        epoch_seconds: float,
        lat: float,
        lon: float,
        sog_ms: float,
        depth_m: Optional[float],
        water_temp_c: Optional[float],
        cog_deg: Optional[float],
    ) -> None:
        self._time.append(epoch_seconds)
        self._lat.append(lat)
        self._lon.append(lon)
        self._sog_ms.append(sog_ms)
        self._depth_m.append(depth_m if depth_m is not None else math.nan)
        self._water_temp_c.append(water_temp_c if water_temp_c is not None else math.nan)
        self._cog_deg.append(cog_deg if cog_deg is not None else math.nan)

    def time_at(self, i: int) -> float:
        return self._time[i]

    def datetime_at(self, i: int) -> datetime:
        return from_epoch(self._time[i])

    def lat_at(self, i: int) -> float:
        return self._lat[i]

    def lon_at(self, i: int) -> float:
        return self._lon[i]

    def sog_at(self, i: int) -> float:
        return self._sog_ms[i]

    def depth_at(self, i: int) -> Optional[float]:
        depth = self._depth_m[i]
        return None if math.isnan(depth) else depth

    def water_temp_at(self, i: int) -> Optional[float]:
        temp = self._water_temp_c[i]
        return None if math.isnan(temp) else temp

    def cog_at(self, i: int) -> Optional[float]:
        cog = self._cog_deg[i]
        return None if math.isnan(cog) else cog

    def index_range_for_time(self, start: datetime, end: datetime) -> Optional[Tuple[int, int]]:
        """First..last index (as a half-open range) whose time falls in [start, end] inclusive,
        via bisect -- relies on rows being appended in non-decreasing time order (always true in
        practice: build_trips()'s only caller, _merge_nav_samples, iterates an already
        outlier-rejected-and-sorted FixArray). Returns None if nothing falls in that window."""
        lo = bisect.bisect_left(self._time, to_epoch(start))
        hi = bisect.bisect_right(self._time, to_epoch(end))
        return (lo, hi) if hi > lo else None

    def __len__(self) -> int:
        return len(self._time)
