"""The decode -> trips pipeline shared by the desktop CLI (cli.py's _run()) and the Android entry
point (android_entry.py's run_pipeline()): decode every .ebl file (through the sample cache),
merge the samples per source, pick the primary GPS, build the trips (reusing already-settled ones
from the trip cache, see trip_cache.py) and resolve their place names. What differs between the
two callers -- reading settings, writing the outputs, uploading, reporting errors -- stays in the
callers; this module only ever raises PipelineError/PipelineCancelled for them to translate.
"""

from __future__ import annotations

import argparse
import gc
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, TypeVar, Union

from .ebl_reader import iter_frames as iter_frames_ebl, restore_time_state, snapshot_time_state
from .fix_array import (
    AttitudeArray,
    BatteryArray,
    DepthArray,
    EngineArray,
    FixArray,
    RpmArray,
    SogArray,
    TripFuelArray,
    WaterTempArray,
)
from .gnss_gate import GnssFixGate
from .log import log
from .model import (
    AttitudeSample,
    BatterySample,
    DepthSample,
    EngineRpmSample,
    EngineSample,
    Frame,
    PositionFix,
    SogSample,
    TripFuelSample,
    WaterTempSample,
)
from .pgn_decode import (
    PGN_ATTITUDE,
    PGN_BATTERY_STATUS,
    PGN_COG_SOG_RAPID,
    PGN_ENGINE_DYNAMIC,
    PGN_ENGINE_RAPID,
    PGN_GNSS_DOPS,
    PGN_POSITION_RAPID,
    PGN_TEMPERATURE,
    PGN_TRIP_FUEL_ENGINE,
    PGN_WATER_DEPTH,
    decode_attitude,
    decode_battery_status,
    decode_cog,
    decode_engine_dynamic,
    decode_engine_rapid,
    decode_gnss_dops,
    decode_position_rapid,
    decode_sea_temperature,
    decode_sog,
    decode_trip_fuel_engine,
    decode_water_depth,
)
from .sample_cache import SampleCache
from .trip_cache import TripCache, choose_resume_index, config_signature, find_resume_index
from .tripbuilder import TRIP_LOGIC_VERSION, TripLeg, build_trips, resolve_trip_places

_T = TypeVar("_T")
_ArrayT = TypeVar("_ArrayT")  # one of fix_array.py's array.array-backed sample collections

# How often (in seconds of wall-clock time, not file count) to log decode progress while working
# through args.logfiles -- a real archive can be 1000+ files, and decoding the ones not already in
# the sample cache is CPU-bound with no other output in between, which on slower hardware (found
# in practice: the Android app's phone CPU, not just a hypothetical) can silently run for minutes
# with nothing on screen to tell a real decode from a hang. Time-based rather than every-N-files:
# self-adapting to however fast decoding actually goes on the hardware it's running on, instead of
# firing constantly on a fast machine or barely at all on a slow one.
_DECODE_PROGRESS_INTERVAL_S = 2.0

# How many times a resumed trip-cache window is allowed to widen by one more file (see the
# "Unknown (start outside log file)" check around the build_trips() call in _run()) before giving
# up and proceeding with whatever it's got -- a stay can only ever be split by a single file
# boundary, so one widening resolves the overwhelming majority of cases; this is purely a
# defensive cap against a pathological repeat, not a value expected to matter in practice.
_MAX_RESUME_WIDEN_ATTEMPTS = 5

# The only PGNs this app decodes anything from (see _collect_samples below). Real NMEA2000
# buses carry a lot of other chatter (autopilot/heading/attitude PGNs can easily outnumber these
# 100:1) that would otherwise get fully decoded and turned into Frame objects for nothing --
# passed to ebl_reader.iter_frames so it can drop everything else right after reading the CAN ID.
_WANTED_PGNS = frozenset(
    {
        PGN_POSITION_RAPID,
        PGN_COG_SOG_RAPID,
        PGN_ENGINE_DYNAMIC,
        PGN_ENGINE_RAPID,
        PGN_GNSS_DOPS,
        PGN_TRIP_FUEL_ENGINE,
        PGN_WATER_DEPTH,
        PGN_TEMPERATURE,
        PGN_BATTERY_STATUS,
        PGN_ATTITUDE,
    }
)


def _dominant_source_only(by_source: Dict[int, _T]) -> Union[_T, list]:
    """Some boats have multiple devices sending the same PGN (e.g. two GPS antennas that both
    send position or speed over ground). Without filtering, their independent, slightly
    differing readings get interleaved purely by time, which causes hundreds of small false
    "jumps" that together can significantly inflate the distance. We therefore only keep the
    source that sent the most messages -- across all files together, not per file, otherwise a
    different source could "win" in each file and the problem would just come back at the seam
    between files."""
    if not by_source:
        return []
    dominant_source = max(by_source, key=lambda source: len(by_source[source]))
    return by_source[dominant_source]


def _merge_array_by_source(target: Dict[int, _ArrayT], addition: Dict[int, list], factory: Callable[[], _ArrayT]) -> None:
    """Accumulates one file's per-source samples into a season-wide, per-source collection --
    one of the array.array-backed types from fix_array.py rather than a plain list; see that
    module's own docstring for why: a season's worth of these held as Python objects instead of
    array.array columns is what actually got the Android app OOM-killed by the phone's OS. Used
    for every sample type that has a season-wide accumulator (position, speed, depth, water
    temperature, battery, attitude); the couple of lower-level, per-file-only lists (e.g. inside
    _collect_samples()) stay plain lists -- their size is bounded by one file, not the whole
    archive, so it isn't worth it there."""
    for source, items in addition.items():
        if source not in target:
            target[source] = factory()
        target[source].extend(items)


def _filter_to_dominant_engine(
    engine_samples: EngineArray,
    trip_fuel_samples: TripFuelArray,
    rpm_samples: RpmArray,
) -> Tuple[EngineArray, TripFuelArray, RpmArray]:
    """Keeps only the engine instance with the most samples, discarding any other instance
    entirely. Used when ``--engine-count 1`` tells us there's really just one physical engine,
    so any additional instance that shows up in the data is noise (a duplicate/ghost source),
    not a second engine -- the same idea as ``_dominant_source_only`` for GPS sources.

    Counts per instance rather than grouping full samples into per-instance lists (the season-
    wide engine_samples/rpm_samples arrays can be hundreds of thousands to millions of samples,
    see EngineArray/RpmArray's own docstrings in fix_array.py) -- a single filtering pass over
    each array, once the dominant instance is known, is enough."""
    counts: Dict[int, int] = {}
    for sample in engine_samples:
        counts[sample.instance] = counts.get(sample.instance, 0) + 1
    if len(counts) <= 1:
        return engine_samples, trip_fuel_samples, rpm_samples

    dominant = max(counts, key=counts.get)
    filtered_engine = EngineArray(sample for sample in engine_samples if sample.instance == dominant)
    filtered_fuel = TripFuelArray(sample for sample in trip_fuel_samples if sample.instance == dominant)
    filtered_rpm = RpmArray(sample for sample in rpm_samples if sample.instance == dominant)
    return filtered_engine, filtered_fuel, filtered_rpm


def _select_primary_gps_source(
    fixes_by_source: Dict[int, FixArray], sogs_by_source: Dict[int, SogArray]
) -> Tuple[FixArray, SogArray, Optional[int]]:
    """Picks one coherent primary GPS source for position AND speed together, instead of
    choosing independently per PGN (as ``_dominant_source_only`` would do on its own).

    Why: measured with real data that two GPS receivers on the same boat give a few meters of
    position difference and a fraction of a knot of speed difference -- small on its own, but
    if the "winning" position source and the "winning" speed source happen to be two different
    physical devices, that creates a hard-to-diagnose inconsistency between the track and the
    stationary/underway classification. Taking speed from the same source as the chosen
    position source keeps that coherent.

    Approach: the source with the most position messages (PGN 129025) leads. Speed from that
    same source is used; only if that source itself didn't send speed does the code fall back
    to the speed source with the most messages (so then a different physical device than the
    position source after all -- better than mixing all sources together, but not ideal)."""
    if not fixes_by_source:
        return FixArray(), _dominant_source_only(sogs_by_source), None

    primary_source = max(fixes_by_source, key=lambda source: len(fixes_by_source[source]))
    fixes = fixes_by_source[primary_source]
    sogs = sogs_by_source.get(primary_source) or _dominant_source_only(sogs_by_source)
    return fixes, sogs, primary_source


def _collect_samples(
    frames: Iterable[Frame],
) -> Tuple[
    Dict[int, List[PositionFix]],
    Dict[int, List[SogSample]],
    List[EngineSample],
    List[TripFuelSample],
    Dict[int, List[DepthSample]],
    Dict[int, List[WaterTempSample]],
    Dict[int, List[BatterySample]],
    List[EngineRpmSample],
    Dict[int, List[AttitudeSample]],
]:
    """Processes frames into samples, grouped by source address for PGNs that can come from
    multiple devices at once. Stops cleanly on Ctrl+C, so an interrupted run still produces a
    logbook of whatever came in up to that point."""
    sogs_by_source: Dict[int, List[SogSample]] = {}
    depth_by_source: Dict[int, List[DepthSample]] = {}
    water_temp_by_source: Dict[int, List[WaterTempSample]] = {}
    battery_by_source: Dict[int, List[BatterySample]] = {}
    attitude_by_source: Dict[int, List[AttitudeSample]] = {}
    engine_samples: List[EngineSample] = []
    trip_fuel_samples: List[TripFuelSample] = []
    rpm_samples: List[EngineRpmSample] = []
    gnss_gate = GnssFixGate()  # see gnss_gate.py: position fixes only count while the receiver has a fix
    try:
        for frame in frames:
            if frame.pgn == PGN_GNSS_DOPS:
                dops = decode_gnss_dops(frame.data)
                if dops is not None:
                    gnss_gate.on_dop(frame.source, *dops)
            elif frame.pgn == PGN_POSITION_RAPID:
                decoded = decode_position_rapid(frame.data)
                if decoded is not None:
                    lat, lon = decoded
                    gnss_gate.add_fix(frame.source, PositionFix(frame.time, lat, lon))
            elif frame.pgn == PGN_COG_SOG_RAPID:
                sog = decode_sog(frame.data)
                if sog is not None:
                    cog = decode_cog(frame.data)
                    sogs_by_source.setdefault(frame.source, []).append(SogSample(frame.time, sog, cog))
            elif frame.pgn == PGN_ENGINE_DYNAMIC:
                decoded = decode_engine_dynamic(frame.data)
                if decoded is not None:
                    engine_samples.append(EngineSample(time=frame.time, **decoded))
            elif frame.pgn == PGN_ENGINE_RAPID:
                decoded = decode_engine_rapid(frame.data)
                if decoded is not None:
                    instance, rpm = decoded
                    rpm_samples.append(EngineRpmSample(frame.time, instance, rpm))
            elif frame.pgn == PGN_TRIP_FUEL_ENGINE:
                decoded = decode_trip_fuel_engine(frame.data)
                if decoded is not None:
                    instance, trip_fuel_l = decoded
                    trip_fuel_samples.append(TripFuelSample(frame.time, instance, trip_fuel_l))
            elif frame.pgn == PGN_WATER_DEPTH:
                depth_m = decode_water_depth(frame.data)
                if depth_m is not None:
                    depth_by_source.setdefault(frame.source, []).append(DepthSample(frame.time, depth_m))
            elif frame.pgn == PGN_TEMPERATURE:
                temp_c = decode_sea_temperature(frame.data)
                if temp_c is not None:
                    water_temp_by_source.setdefault(frame.source, []).append(WaterTempSample(frame.time, temp_c))
            elif frame.pgn == PGN_BATTERY_STATUS:
                decoded = decode_battery_status(frame.data)
                if decoded is not None:
                    instance, voltage_v = decoded
                    battery_by_source.setdefault(frame.source, []).append(
                        BatterySample(frame.time, instance, voltage_v)
                    )
            elif frame.pgn == PGN_ATTITUDE:
                decoded = decode_attitude(frame.data)
                if decoded is not None:
                    pitch_deg, roll_deg = decoded
                    attitude_by_source.setdefault(frame.source, []).append(
                        AttitudeSample(frame.time, pitch_deg, roll_deg)
                    )
    except KeyboardInterrupt:
        log("[info] Interrupted by user; writing the logbook with the data collected so far...", file=sys.stderr)
    gnss_gate.finish()
    fixes_by_source = gnss_gate.accepted
    return (
        fixes_by_source,
        sogs_by_source,
        engine_samples,
        trip_fuel_samples,
        depth_by_source,
        water_temp_by_source,
        battery_by_source,
        rpm_samples,
        attitude_by_source,
    )


def _iter_frames_for_path(
    path: Path, ebl_time_state: Optional[Dict[str, object]] = None
) -> Iterable[Frame]:
    """``ebl_time_state`` is passed to consecutive .ebl files so the last known time (PGN 126992)
    is preserved across file boundaries -- otherwise a file without its own System Time message
    (e.g. anchored for a long time, GPS/plotter idle) gets discarded entirely, even though the
    time is already known from the previous file (see ebl_reader.py). The dict holds more than
    that time (which device's clock is trusted follows from per-source message counts), so a run
    that continues from a cache restores all of it, see restore_time_state()."""
    return iter_frames_ebl(path, time_state=ebl_time_state, wanted_pgns=_WANTED_PGNS)


def discover_ebl_files(ebl_dir: Path) -> List[Path]:
    """Every .ebl file found recursively under ``ebl_dir``, sorted -- used when nmea2log is
    called without any logfiles (see --ebl-dir), so you don't have to select or drag files by
    hand after downloading them."""
    return sorted(ebl_dir.rglob("*.ebl"))


class PipelineError(Exception):
    """A run that can't produce a logbook (a missing file, no position data, no trips); the
    message is what the caller reports."""


class PipelineCancelled(Exception):
    """``should_cancel()`` said so partway through decoding."""


@dataclass
class SeasonTrips:
    trips: List[TripLeg]
    latest_position: Optional[PositionFix]  # the newest GPS fix decoded this run, if any


def build_season_trips(
    logfiles: List[Path],
    args: argparse.Namespace,
    *,
    sample_cache_path: Optional[Path],
    trip_cache_path: Optional[Path],
    geocoder: object,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> SeasonTrips:
    """Decodes ``logfiles`` (already in chronological order -- both the resume cache below and
    every season-wide sample array assume file order is time order) and builds the season's trips.
    ``args`` supplies every trip-building threshold (see build_arg_parser()); the two cache paths
    may be None to skip that cache. ``should_cancel``, if given, is checked before every file.

    Raises PipelineError/PipelineCancelled instead of returning an error, so each caller can
    report it its own way."""
    # Computed from every build_trips() parameter below (plus anything else that changes what a
    # cached trip looks like, e.g. the geocoding language, or the trip-building logic itself --
    # see TRIP_LOGIC_VERSION in tripbuilder.py) *before* the trip cache is loaded, so a cache
    # built under different settings -- or different code -- is never silently trusted -- see
    # config_signature() in trip_cache.py.
    trip_signature = config_signature(
        speed_threshold_kn=args.speed_threshold_kn,
        min_stop_minutes=args.min_stop_minutes,
        max_gap_minutes=args.max_gap_minutes,
        min_trip_distance_nm=args.min_trip_distance_nm,
        min_leg_distance_nm=args.min_leg_distance_nm,
        lock_radius_m=args.lock_radius_m,
        lock_max_duration_minutes=args.lock_max_duration_minutes,
        no_geocode=args.no_geocode,
        language=args.language,
        engine_count=args.engine_count,
        trip_logic_version=TRIP_LOGIC_VERSION,
    )
    trip_cache_store = None if trip_cache_path is None else TripCache(trip_cache_path)
    settled_trips: list = []
    resume_index = 0  # first file index this run actually needs to decode -- 0 unless a usable
    # trip cache says otherwise, i.e. every file is decoded exactly like before trip caching existed
    resume_ebl_time_state_seed: object = None
    if trip_cache_store is not None:
        cached_trip_data = trip_cache_store.load(trip_signature)
        if cached_trip_data is not None:
            cached_settled_trips, resume_from_file, resume_ebl_time_state = cached_trip_data
            found_index = find_resume_index(logfiles, resume_from_file)
            if found_index is None:
                log(
                    "[cache] Trip cache's resume file isn't among the given logfiles anymore -- "
                    "rebuilding all trips from scratch.",
                    file=sys.stderr,
                )
            else:
                settled_trips = cached_settled_trips
                resume_index = found_index
                resume_ebl_time_state_seed = resume_ebl_time_state
                log(
                    f"[cache] Reusing {len(settled_trips)} already-settled trip(s); skipping "
                    f"decode of {resume_index}/{len(logfiles)} file(s), resuming from "
                    f"{logfiles[resume_index].name}.",
                    file=sys.stderr,
                )

    sample_cache = None if sample_cache_path is None else SampleCache(sample_cache_path)

    # Retried with resume_index widened by one file at a time -- see the check right after
    # build_trips() below -- if a resumed window turns out to have started mid-transit rather
    # than genuinely at the start of a stay. Everything inside this loop is scoped to a single
    # attempt and rebuilt from scratch each time; only settled_trips/resume_index/geocoder/
    # sample_cache carry over between attempts.
    widen_attempts = 0
    while True:
        # Every season-wide accumulator here is one of the array.array-backed types from
        # fix_array.py rather than a plain list -- a season's worth of these held as boxed Python
        # objects instead of array.array columns is what actually got the Android app OOM-killed
        # by the phone's OS (confirmed in practice, on a real ~2326-file archive: engine ~400k
        # samples, RPM ~1.75 million -- comparable cardinality to position/speed, not negligible).
        # EngineArray's own ``warnings`` field (a FrozenSet[str], the one field of the bunch that
        # doesn't map onto a fixed-width array column) is stored sparsely inside it instead --
        # see its own docstring.
        fixes_by_source: Dict[int, FixArray] = {}
        sogs_by_source: Dict[int, SogArray] = {}
        depth_by_source: Dict[int, DepthArray] = {}
        water_temp_by_source: Dict[int, WaterTempArray] = {}
        battery_by_source: Dict[int, BatteryArray] = {}
        attitude_by_source: Dict[int, AttitudeArray] = {}
        all_engine = EngineArray()
        all_trip_fuel = TripFuelArray()
        all_rpm = RpmArray()

        # The cache's own recorded seed only applies to the *original* resume point -- a widened
        # attempt starts one file earlier than that, whose own carried-over PGN 126992 time was
        # never captured (the first attempt only ever started tracking it from the original
        # resume point onward). Starting that earlier file with no seed at all, same as the very
        # first file of a full run, is an honest, small degradation (its first frame or two could
        # miss a timestamp if it has no System Time message of its own before its next one) --
        # preferable to seeding it with a value that actually belongs to a different file.
        ebl_time_state: Dict[str, object] = {}
        if widen_attempts == 0 and resume_ebl_time_state_seed is not None:
            restore_time_state(ebl_time_state, resume_ebl_time_state_seed)

        cache_hits = 0
        last_progress_log = time.monotonic()
        # Only populated for file indices actually decoded this attempt (>= resume_index) -- used
        # purely to pick where the *next* run should resume from, see choose_resume_index() in
        # trip_cache.py.
        file_first_time_by_index: Dict[int, Optional[datetime]] = {}
        ebl_time_state_before_file: Dict[int, object] = {}
        for idx, path in enumerate(logfiles, start=1):
            # Checked before the resume skip below too: found in practice, closing the Android app
            # had no effect at all once decoding started, since only the earlier download loop
            # ever checked for it -- a long decode (1000+ files, on a phone's slower CPU) could
            # run on for many more minutes after the user thought they'd stopped it.
            if should_cancel is not None and should_cancel():
                raise PipelineCancelled()
            file_index = idx - 1
            if file_index < resume_index:
                # Already fully represented by settled_trips -- never even touched, not even to
                # check the sample cache, which is exactly the decode-time and decompression cost
                # this whole trip cache exists to avoid paying every single run.
                continue
            if not path.exists():
                raise PipelineError(f"Log file not found: {path}")

            ebl_time_state_before_file[file_index] = snapshot_time_state(ebl_time_state)

            cached = sample_cache.get(path) if sample_cache is not None else None
            if cached is not None:
                samples, time_state_after = cached
                fixes, sogs, engine, trip_fuel, depth, water_temp, battery, rpm, attitude = samples
                restore_time_state(ebl_time_state, time_state_after)
                cache_hits += 1
            else:
                frames = _iter_frames_for_path(path, ebl_time_state)
                samples = _collect_samples(frames)
                fixes, sogs, engine, trip_fuel, depth, water_temp, battery, rpm, attitude = samples
                if sample_cache is not None:
                    sample_cache.put(path, samples, snapshot_time_state(ebl_time_state))

            file_first_time: Optional[datetime] = None
            for source_fixes in fixes.values():
                if source_fixes:
                    candidate_time = min(f.time for f in source_fixes)
                    file_first_time = candidate_time if file_first_time is None else min(file_first_time, candidate_time)
            file_first_time_by_index[file_index] = file_first_time

            # Time-based rather than every-N-files -- see _DECODE_PROGRESS_INTERVAL_S.
            now = time.monotonic()
            if now - last_progress_log >= _DECODE_PROGRESS_INTERVAL_S and idx < len(logfiles):
                log(f"[info] ...decoded {idx}/{len(logfiles)} logfile(s) so far", file=sys.stderr)
                last_progress_log = now

            _merge_array_by_source(fixes_by_source, fixes, FixArray)
            _merge_array_by_source(sogs_by_source, sogs, SogArray)
            all_engine.extend(engine)
            all_trip_fuel.extend(trip_fuel)
            _merge_array_by_source(depth_by_source, depth, DepthArray)
            _merge_array_by_source(water_temp_by_source, water_temp, WaterTempArray)
            _merge_array_by_source(battery_by_source, battery, BatteryArray)
            all_rpm.extend(rpm)
            _merge_array_by_source(attitude_by_source, attitude, AttitudeArray)

        # Unconditional, unlike the in-loop progress line above (which deliberately skips the very
        # last file so it doesn't fire right before this same count gets logged again a few lines
        # down) -- found in practice: since that in-loop line is also time-gated, the *previous*
        # progress line could be a couple of seconds stale even when decode genuinely finished
        # cleanly, leaving no explicit confirmation the last file (specifically) was ever reached
        # rather than the run having silently died one file short.
        log(f"[info] ...decoded {len(logfiles)}/{len(logfiles)} logfile(s) so far", file=sys.stderr)

        decoded_file_count = len(logfiles) - resume_index
        if sample_cache is not None and cache_hits:
            log(
                f"[cache] reused decoded samples for {cache_hits}/{decoded_file_count} file(s), "
                f"only re-parsed {decoded_file_count - cache_hits}",
                file=sys.stderr,
            )

        all_fixes, all_sogs, primary_gps_source = _select_primary_gps_source(fixes_by_source, sogs_by_source)
        all_depth = _dominant_source_only(depth_by_source)
        all_water_temp = _dominant_source_only(water_temp_by_source)
        all_battery = _dominant_source_only(battery_by_source)
        all_attitude = _dominant_source_only(attitude_by_source)

        if len(fixes_by_source) > 1:
            log(
                f"[info] Multiple position sources found ({sorted(fixes_by_source)}); "
                f"using source {primary_gps_source} as the primary GPS (most messages).",
                file=sys.stderr,
            )

        # The *_by_source dicts are dead weight from here on: each all_* variable already holds
        # its own direct reference to the one source array it needs (see _select_primary_gps_source
        # /_dominant_source_only -- "by_source[dominant]", not a copy), so the dicts themselves now
        # only hold every *non*-dominant source's full array for nothing. On a boat with more than
        # one device sending the same PGN (e.g. two GPS antennas), that's real, otherwise-
        # unreachable memory -- found in practice: a real OOM kill (lmkd reaped the Android app at
        # ~2.6 GB rss) landed inside build_trips() itself just below, the single most memory-hungry
        # phase of the whole run, so freeing this now buys real headroom right where it matters.
        del fixes_by_source, sogs_by_source, depth_by_source, water_temp_by_source, battery_by_source
        del attitude_by_source
        gc.collect()

        if not all_fixes and not settled_trips:
            raise PipelineError("No position data (PGN 129025) found.")

        if args.engine_count == 1:
            all_engine, all_trip_fuel, all_rpm = _filter_to_dominant_engine(all_engine, all_trip_fuel, all_rpm)

        if all_fixes:
            # Found in practice: build_trips() (and write_html_logbook() after it) can run for a
            # real stretch of time on a full multi-year archive (millions of merged GPS fixes)
            # with zero log output in between -- decode's own progress logging (see
            # _DECODE_PROGRESS_INTERVAL_S) stops the moment the last file is read, leaving nothing
            # on screen to distinguish "still working" from "hung" or "already crashed silently"
            # for however long this phase takes (much longer on a phone's CPU).
            log(f"[info] Building trips from {len(all_fixes)} GPS position(s)...", file=sys.stderr)
            fresh_trips = build_trips(
                all_fixes,
                all_sogs,
                all_engine,
                all_trip_fuel,
                all_depth,
                all_water_temp,
                all_battery,
                all_rpm,
                all_attitude,
                speed_threshold_kn=args.speed_threshold_kn,
                min_stop_minutes=args.min_stop_minutes,
                max_gap_minutes=args.max_gap_minutes,
                min_trip_distance_nm=args.min_trip_distance_nm,
                min_leg_distance_nm=args.min_leg_distance_nm,
                lock_radius_m=args.lock_radius_m if args.lock_radius_m >= 0 else None,
                lock_max_duration_minutes=args.lock_max_duration_minutes,
            )
        else:
            # The reprocessed window (from the last known trip's own departure onward, see
            # resume_index above) happened to contain no position data at all -- e.g. everything
            # since then is still exactly the one already-fully-decoded file it resumed from, with
            # nothing new after it yet. Nothing left to (re)build; settled_trips alone, below, is
            # still a perfectly good result.
            fresh_trips = []

        # A resumed window (resume_index > 0, i.e. genuinely picking up from cached history, not
        # a full from-scratch run) whose own first trip has no known stay before it means the
        # window itself started mid-transit, not genuinely at the very first thing in the whole
        # archive -- build_trips() has no way to tell those two situations apart from inside a
        # single call (see its own "Unknown (start outside log file)" fallback), since it only
        # ever sees whatever window it was given. Confirmed in practice, on real data: a resumed
        # window that started this way produced junk/duplicate trips right at its own boundary.
        # Re-decoding with one more file of history resolves it in the overwhelming majority of
        # cases -- a stay can only ever be split by a single file boundary. Capped defensively
        # (_MAX_RESUME_WIDEN_ATTEMPTS) so a pathological repeat can't loop forever; the run then
        # just proceeds with whatever it's got, no worse off than before this widening existed.
        #
        # Deliberately NOT gated on settled_trips still being non-empty (only used below, as a
        # side effect, to decide whether there's anything left to pop) -- a first widen attempt
        # can itself still leave the *new* window starting mid-transit too (e.g. the transit
        # itself spans more than one file), and by then settled_trips may already be empty from
        # the previous attempt's own pop below; refusing to widen further at that point would
        # silently accept a still-wrong result purely because there happened to be nothing left
        # to un-freeze, even though decoding still more history remains both safe (nothing left
        # in settled_trips to double-count) and potentially still corrective.
        if (
            resume_index > 0
            and fresh_trips
            and fresh_trips[0].depart_place == "Unknown (start outside log file)"
            and widen_attempts < _MAX_RESUME_WIDEN_ATTEMPTS
        ):
            widen_attempts += 1
            resume_index -= 1
            if settled_trips and widen_attempts == 1:
                # The boundary settled_trips' own last entry was frozen at turned out to be wrong
                # (see above) -- that trip's data is now included in the widened window and will
                # be reconstructed fresh as part of fresh_trips instead, so it must not also
                # survive here, or it would show up twice. Popped exactly once, on the *first*
                # widen only -- every subsequent widen this same run is still about resolving
                # that same one trip's boundary (e.g. its own transit itself spans more than one
                # file), not a second, different trip to also un-freeze (found in practice, on
                # real data: popping on every attempt instead silently discarded several already-
                # correct trips whose own boundaries were never actually in question).
                settled_trips = settled_trips[:-1]
            log(
                f"[cache] Resumed window started mid-transit (no known stay before its first "
                f"trip) -- widening by one file ({logfiles[resume_index].name}) and "
                f"retrying (attempt {widen_attempts}/{_MAX_RESUME_WIDEN_ATTEMPTS}).",
                file=sys.stderr,
            )
            continue
        break

    # Freed once, after the loop: the sample cache stays alive across a widened retry above.
    del sample_cache
    gc.collect()

    trips = settled_trips + fresh_trips

    if not trips:
        raise PipelineError("No trips found (maybe never stopped or underway long enough relative to the thresholds).")

    if trip_cache_store is not None and fresh_trips:
        # Every trip except the newest one is now eligible to freeze -- see the module docstring
        # in trip_cache.py for why the newest trip specifically is never cached, even here.
        new_settled_trips = settled_trips + fresh_trips[:-1]
        # The reference point for where the *next* run can safely resume from is the arrival of
        # the trip right before the still-open last one -- i.e. the start of the stay that last
        # trip departs from -- not that last trip's own departure. See choose_resume_index() in
        # trip_cache.py for why using anything later than this reintroduces an already-settled
        # trip a second time. Falls back to the last trip's own departure only when there's no
        # earlier trip at all yet (nothing before it that could be duplicated).
        resume_reference_time = (
            new_settled_trips[-1].arrive_time if new_settled_trips else fresh_trips[-1].depart_time
        )
        new_resume_index = choose_resume_index(
            sorted(file_first_time_by_index.items()), resume_reference_time, resume_index
        )
        new_resume_file = str(logfiles[new_resume_index].resolve())
        new_resume_state = ebl_time_state_before_file.get(new_resume_index)
        trip_cache_store.save(new_settled_trips, new_resume_file, new_resume_state, trip_signature)

    # Every run, over every trip -- settled (just loaded straight from trip_cache_store.load()
    # above, never touched by build_trips() at all this run) included, not just the fresh ones --
    # see resolve_trip_places()'s own doc comment for why this can't just happen once inside
    # build_trips() and get cached alongside everything else about a settled trip.
    trips = resolve_trip_places(trips, geocoder)

    # trips (a small, already-summarized list of TripLeg) is everything the caller needs -- the
    # season's worth of raw per-sample arrays that built it are pure dead weight from here on,
    # except the one still-needed latest position, captured first. Same reasoning as the
    # *_by_source cleanup above: real headroom for the phase after this, which runs real (not
    # instant) geocoding/weather/marine network lookups per trip.
    latest_position = all_fixes[-1] if all_fixes else None
    del all_fixes, all_sogs, all_depth, all_water_temp, all_battery, all_attitude
    del all_engine, all_trip_fuel, all_rpm
    gc.collect()

    return SeasonTrips(trips, latest_position)
