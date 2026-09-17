"""Columnar, array.array-backed storage for a season's worth of PositionFix/SogSample -- built to
replace the plain Python lists cli.py/android_entry.py used to accumulate across an entire
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
import math
from datetime import datetime, timedelta
from typing import Iterable, Iterator, List, Optional, Union

from .model import AttitudeSample, BatterySample, DepthSample, PositionFix, SogSample, WaterTempSample

_EPOCH = datetime(1970, 1, 1)


def _to_epoch(dt: datetime) -> float:
    return (dt - _EPOCH).total_seconds()


def _from_epoch(seconds: float) -> datetime:
    return _EPOCH + timedelta(seconds=seconds)


class FixArray:
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
        self.append_raw(_to_epoch(fix.time), fix.lat, fix.lon)

    def append_raw(self, epoch_seconds: float, lat: float, lon: float) -> None:
        """Appends an already-epoch-encoded row directly -- used internally (see
        tripbuilder.py's _reject_gps_outliers_array) to build a filtered/reordered FixArray
        without ever reconstructing a PositionFix object in between."""
        self._time.append(epoch_seconds)
        self._lat.append(lat)
        self._lon.append(lon)

    def extend(self, fixes: Iterable[PositionFix]) -> None:
        for fix in fixes:
            self.append(fix)

    def time_at(self, i: int) -> float:
        return self._time[i]

    def lat_at(self, i: int) -> float:
        return self._lat[i]

    def lon_at(self, i: int) -> float:
        return self._lon[i]

    def datetime_at(self, i: int) -> datetime:
        return _from_epoch(self._time[i])

    def __len__(self) -> int:
        return len(self._lat)

    def __getitem__(self, i: int) -> PositionFix:
        # array.array already supports negative indices natively -- e.g. all_fixes[-1] for "the
        # most recent fix in the whole dataset" (see cli.py/android_entry.py).
        return PositionFix(_from_epoch(self._time[i]), self._lat[i], self._lon[i])

    def __iter__(self) -> Iterator[PositionFix]:
        for t, lat, lon in zip(self._time, self._lat, self._lon):
            yield PositionFix(_from_epoch(t), lat, lon)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FixArray):
            return list(self) == list(other)
        if isinstance(other, list):
            return list(self) == other
        return NotImplemented

    def __repr__(self) -> str:
        return f"FixArray({list(self)!r})"

    def slice_by_time(self, start_epoch: float, end_epoch: float) -> "FixArray":
        """Fixes with start_epoch <= time < end_epoch, as a new FixArray -- used by
        build_trips_chunked() to carve a time window's worth of a season out of the full archive
        without ever materializing the fixes outside that window as PositionFix objects just to
        filter them. Requires self to already be time-sorted -- call sorted_by_time() first if
        that's not already guaranteed (found in practice: NOT true of every real caller --
        android_entry.py builds this from files discovered via Kotlin's File.walkTopDown(), which
        makes no ordering guarantee at all, unlike cli.py's own sorted glob() -- a real archive
        this way came back internally out of order by nearly three weeks). Uses bisect, not a
        linear scan, since a season can be millions of fixes."""
        lo = bisect.bisect_left(self._time, start_epoch)
        hi = bisect.bisect_left(self._time, end_epoch)
        result = FixArray()
        result._time = self._time[lo:hi]
        result._lat = self._lat[lo:hi]
        result._lon = self._lon[lo:hi]
        return result

    def sorted_by_time(self) -> "FixArray":
        """Array-native equivalent of sorted(fixes, key=lambda f: f.time) -- build_trips_chunked()
        calls this once up front (see its own doc comment on why input order can't be trusted),
        so a season's worth of fixes never has to be materialized into a plain list of
        PositionFix objects just to sort it, the same problem SogArray.sorted_by_time() already
        solved for SOG."""
        order = sorted(range(len(self)), key=self.time_at)
        result = FixArray()
        for i in order:
            result._time.append(self._time[i])
            result._lat.append(self._lat[i])
            result._lon.append(self._lon[i])
        return result


class SogArray:
    """Same idea as FixArray, for SogSample. cog_deg is Optional in SogSample -- stored as NaN
    when absent, since it's already a float field and NaN never legitimately occurs otherwise."""

    __slots__ = ("_time", "_sog_ms", "_cog_deg")

    def __init__(self, sogs: Iterable[SogSample] = ()) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._sog_ms: "array.array[float]" = array.array("d")
        self._cog_deg: "array.array[float]" = array.array("d")
        self.extend(sogs)

    def append(self, sog: SogSample) -> None:
        self._time.append(_to_epoch(sog.time))
        self._sog_ms.append(sog.sog_ms)
        self._cog_deg.append(sog.cog_deg if sog.cog_deg is not None else math.nan)

    def extend(self, sogs: Iterable[SogSample]) -> None:
        for sog in sogs:
            self.append(sog)

    def time_at(self, i: int) -> float:
        return self._time[i]

    def sog_at(self, i: int) -> float:
        return self._sog_ms[i]

    def cog_at(self, i: int) -> Optional[float]:
        cog = self._cog_deg[i]
        return None if math.isnan(cog) else cog

    def __len__(self) -> int:
        return len(self._sog_ms)

    def __iter__(self) -> Iterator[SogSample]:
        for t, sog_ms, cog in zip(self._time, self._sog_ms, self._cog_deg):
            yield SogSample(_from_epoch(t), sog_ms, None if math.isnan(cog) else cog)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SogArray):
            return list(self) == list(other)
        if isinstance(other, list):
            return list(self) == other
        return NotImplemented

    def __repr__(self) -> str:
        return f"SogArray({list(self)!r})"

    def sorted_by_time(self) -> "SogArray":
        """Array-native equivalent of sorted(sogs, key=lambda s: s.time) -- used by
        _merge_nav_samples() (see tripbuilder.py) so a season's worth of SOG samples (typically
        similar cardinality to position fixes -- millions, on a real multi-year archive) never
        has to be materialized into a plain list of SogSample objects just to sort it, the same
        problem FixArray's own outlier-rejection pass already solved for position fixes."""
        order = sorted(range(len(self)), key=self.time_at)
        result = SogArray()
        for i in order:
            result._time.append(self._time[i])
            result._sog_ms.append(self._sog_ms[i])
            result._cog_deg.append(self._cog_deg[i])
        return result

    def slice_by_time(self, start_epoch: float, end_epoch: float) -> "SogArray":
        """Same idea as FixArray.slice_by_time() -- requires self to already be time-sorted (call
        sorted_by_time() first if that's not already guaranteed)."""
        lo = bisect.bisect_left(self._time, start_epoch)
        hi = bisect.bisect_left(self._time, end_epoch)
        result = SogArray()
        result._time = self._time[lo:hi]
        result._sog_ms = self._sog_ms[lo:hi]
        result._cog_deg = self._cog_deg[lo:hi]
        return result


class DepthArray:
    """Same idea as FixArray, for DepthSample. depth_m is Optional -- stored as NaN when absent."""

    __slots__ = ("_time", "_depth_m")

    def __init__(self, samples: Iterable[DepthSample] = ()) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._depth_m: "array.array[float]" = array.array("d")
        self.extend(samples)

    def append(self, sample: DepthSample) -> None:
        self._time.append(_to_epoch(sample.time))
        self._depth_m.append(sample.depth_m if sample.depth_m is not None else math.nan)

    def extend(self, samples: Iterable[DepthSample]) -> None:
        for sample in samples:
            self.append(sample)

    def __len__(self) -> int:
        return len(self._depth_m)

    def __iter__(self) -> Iterator[DepthSample]:
        for t, depth_m in zip(self._time, self._depth_m):
            yield DepthSample(_from_epoch(t), None if math.isnan(depth_m) else depth_m)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DepthArray):
            return list(self) == list(other)
        if isinstance(other, list):
            return list(self) == other
        return NotImplemented

    def __repr__(self) -> str:
        return f"DepthArray({list(self)!r})"


class WaterTempArray:
    """Same idea as FixArray, for WaterTempSample. temp_c is Optional -- stored as NaN when
    absent."""

    __slots__ = ("_time", "_temp_c")

    def __init__(self, samples: Iterable[WaterTempSample] = ()) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._temp_c: "array.array[float]" = array.array("d")
        self.extend(samples)

    def append(self, sample: WaterTempSample) -> None:
        self._time.append(_to_epoch(sample.time))
        self._temp_c.append(sample.temp_c if sample.temp_c is not None else math.nan)

    def extend(self, samples: Iterable[WaterTempSample]) -> None:
        for sample in samples:
            self.append(sample)

    def __len__(self) -> int:
        return len(self._temp_c)

    def __iter__(self) -> Iterator[WaterTempSample]:
        for t, temp_c in zip(self._time, self._temp_c):
            yield WaterTempSample(_from_epoch(t), None if math.isnan(temp_c) else temp_c)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, WaterTempArray):
            return list(self) == list(other)
        if isinstance(other, list):
            return list(self) == other
        return NotImplemented

    def __repr__(self) -> str:
        return f"WaterTempArray({list(self)!r})"


class BatteryArray:
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
        self._time.append(_to_epoch(sample.time))
        self._instance.append(sample.instance)
        self._voltage_v.append(sample.voltage_v if sample.voltage_v is not None else math.nan)

    def extend(self, samples: Iterable[BatterySample]) -> None:
        for sample in samples:
            self.append(sample)

    def __len__(self) -> int:
        return len(self._voltage_v)

    def __iter__(self) -> Iterator[BatterySample]:
        for t, instance, voltage_v in zip(self._time, self._instance, self._voltage_v):
            yield BatterySample(_from_epoch(t), int(instance), None if math.isnan(voltage_v) else voltage_v)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, BatteryArray):
            return list(self) == list(other)
        if isinstance(other, list):
            return list(self) == other
        return NotImplemented

    def __repr__(self) -> str:
        return f"BatteryArray({list(self)!r})"


class AttitudeArray:
    """Same idea as FixArray, for AttitudeSample. pitch_deg/roll_deg are Optional -- stored as
    NaN when absent.

    Also supports integer AND slice indexing (unlike the other array types above) -- unlike
    depth/water_temp/battery, which just get iterated in full, _motion_variation() in
    tripbuilder.py narrows this one down to a single trip's own time window with
    bisect.bisect_left/right (needs random-access indexing) followed by a slice, once per trip,
    since a real season can have millions of attitude samples and re-scanning all of them per
    trip was real, measured cost (see that function's own docstring)."""

    __slots__ = ("_time", "_pitch_deg", "_roll_deg")

    def __init__(self, samples: Iterable[AttitudeSample] = ()) -> None:
        self._time: "array.array[float]" = array.array("d")
        self._pitch_deg: "array.array[float]" = array.array("d")
        self._roll_deg: "array.array[float]" = array.array("d")
        self.extend(samples)

    def append(self, sample: AttitudeSample) -> None:
        self._time.append(_to_epoch(sample.time))
        self._pitch_deg.append(sample.pitch_deg if sample.pitch_deg is not None else math.nan)
        self._roll_deg.append(sample.roll_deg if sample.roll_deg is not None else math.nan)

    def extend(self, samples: Iterable[AttitudeSample]) -> None:
        for sample in samples:
            self.append(sample)

    def time_at(self, i: int) -> float:
        return self._time[i]

    def __len__(self) -> int:
        return len(self._time)

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
            _from_epoch(self._time[key]),
            None if math.isnan(pitch) else pitch,
            None if math.isnan(roll) else roll,
        )

    def __iter__(self) -> Iterator[AttitudeSample]:
        for t, pitch, roll in zip(self._time, self._pitch_deg, self._roll_deg):
            yield AttitudeSample(
                _from_epoch(t), None if math.isnan(pitch) else pitch, None if math.isnan(roll) else roll
            )

    def __eq__(self, other: object) -> bool:
        if isinstance(other, AttitudeArray):
            return list(self) == list(other)
        if isinstance(other, list):
            return list(self) == other
        return NotImplemented

    def __repr__(self) -> str:
        return f"AttitudeArray({list(self)!r})"

    def slice_by_time(self, start_epoch: float, end_epoch: float) -> "AttitudeArray":
        """Same idea as FixArray.slice_by_time() -- requires self to already be time-sorted (call
        sorted_by_time() first if that's not already guaranteed). Built on this class's own
        slice-supporting __getitem__ rather than duplicating it column by column."""
        lo = bisect.bisect_left(self._time, start_epoch)
        hi = bisect.bisect_left(self._time, end_epoch)
        return self[lo:hi]

    def sorted_by_time(self) -> "AttitudeArray":
        """Equivalent of sorted(attitude_samples, key=lambda s: s.time) -- build_trips() sorts
        attitude samples once up front (see its own docstring on _motion_variation)."""
        order = sorted(range(len(self)), key=self.time_at)
        result = AttitudeArray()
        for i in order:
            result._time.append(self._time[i])
            result._pitch_deg.append(self._pitch_deg[i])
            result._roll_deg.append(self._roll_deg[i])
        return result
