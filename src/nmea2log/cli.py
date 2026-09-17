from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

from .config import load_section
from .ebl_reader import iter_frames as iter_frames_ebl
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
from .geocode import Geocoder, NoGeocoder
from .marine import MarineFetcher, NoMarine
from .weather import NoWeather, WeatherFetcher
from .gpx_writer import write_gpx
from .html_writer import _DEFAULT_LOG_INTERVAL_MINUTES, _DEFAULT_REMARKS_API_URL, write_html_logbook
from .log import DEFAULT_LOG_RETENTION_DAYS, log, set_log_file, set_log_level
from .logbook_writer import write_csv
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
from .sample_cache import SampleCache
from .trip_cache import TripCache, choose_resume_index, config_signature, find_resume_index
from .pgn_decode import (
    PGN_ATTITUDE,
    PGN_BATTERY_STATUS,
    PGN_COG_SOG_RAPID,
    PGN_ENGINE_DYNAMIC,
    PGN_ENGINE_RAPID,
    PGN_POSITION_RAPID,
    PGN_TEMPERATURE,
    PGN_TRIP_FUEL_ENGINE,
    PGN_WATER_DEPTH,
    decode_attitude,
    decode_battery_status,
    decode_cog,
    decode_engine_dynamic,
    decode_engine_rapid,
    decode_position_rapid,
    decode_sea_temperature,
    decode_sog,
    decode_trip_fuel_engine,
    decode_water_depth,
)
from .trip_ids import assign_trip_ids
from .tripbuilder import TRIP_LOGIC_VERSION, build_trips, resolve_trip_places
from .upload import UploadError, upload_file, upload_via_rest

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
        PGN_TRIP_FUEL_ENGINE,
        PGN_WATER_DEPTH,
        PGN_TEMPERATURE,
        PGN_BATTERY_STATUS,
        PGN_ATTITUDE,
    }
)


def _dominant_source_only(by_source: Dict[int, List[_T]]) -> List[_T]:
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


def _merge_by_source(target: Dict[int, List[_T]], addition: Dict[int, List[_T]]) -> None:
    for source, items in addition.items():
        target.setdefault(source, []).extend(items)


def _merge_array_by_source(target: Dict[int, _ArrayT], addition: Dict[int, list], factory: Callable[[], _ArrayT]) -> None:
    """Same idea as _merge_by_source() above, but accumulating into one of the array.array-backed
    types from fix_array.py instead of a plain list -- see that module's own docstring for why:
    a season's worth of these held as Python objects instead of array.array columns is what
    actually got the Android app OOM-killed by the phone's OS. Used for every sample type that
    has a season-wide accumulator (position, speed, depth, water temperature, battery, attitude);
    the couple of lower-level, per-file-only lists (e.g. inside _collect_samples()) stay plain
    lists -- their size is bounded by one file, not the whole archive, so it isn't worth it there."""
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
    fixes_by_source: Dict[int, List[PositionFix]] = {}
    sogs_by_source: Dict[int, List[SogSample]] = {}
    depth_by_source: Dict[int, List[DepthSample]] = {}
    water_temp_by_source: Dict[int, List[WaterTempSample]] = {}
    battery_by_source: Dict[int, List[BatterySample]] = {}
    attitude_by_source: Dict[int, List[AttitudeSample]] = {}
    engine_samples: List[EngineSample] = []
    trip_fuel_samples: List[TripFuelSample] = []
    rpm_samples: List[EngineRpmSample] = []
    try:
        for frame in frames:
            if frame.pgn == PGN_POSITION_RAPID:
                decoded = decode_position_rapid(frame.data)
                if decoded is not None:
                    lat, lon = decoded
                    fixes_by_source.setdefault(frame.source, []).append(PositionFix(frame.time, lat, lon))
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
    time is already known from the previous file (see ebl_reader.py)."""
    return iter_frames_ebl(path, time_state=ebl_time_state, wanted_pgns=_WANTED_PGNS)


def _discover_ebl_files(ebl_dir: Path) -> List[Path]:
    """Every .ebl file found recursively under ``ebl_dir``, sorted -- used when nmea2log is
    called without any logfiles (see --ebl-dir), so you don't have to select or drag files by
    hand after downloading them."""
    return sorted(ebl_dir.rglob("*.ebl"))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nmea2log",
        description="Turn NMEA2000 log files from an Actisense W2K-2 into a sailing logbook (CSV).",
    )
    parser.add_argument(
        "logfiles",
        nargs="*",
        type=Path,
        help="One or more .ebl log files (SD card log from the W2K-2). If omitted, falls back to "
        "every .ebl file found under --ebl-dir.",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("logbook.csv"), help="Path to the CSV file (default: logbook.csv)"
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Also write the CSV logbook (default: only the HTML logbook is written)",
    )
    parser.add_argument(
        "--gpx",
        action="store_true",
        help="Also write the GPX route file (default: only the HTML logbook is written)",
    )
    parser.add_argument(
        "--log-retention-days",
        type=float,
        default=DEFAULT_LOG_RETENTION_DAYS,
        help=f"How long to keep lines in nmea2log.log (next to the output file) before they're "
        f"automatically dropped -- otherwise that file grows forever (default "
        f"{DEFAULT_LOG_RETENTION_DAYS:g} days)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show routine per-item detail too (every already-local .ebl file skipped, ...), "
        "not just the per-run summary lines -- this detail is always in nmea2log.log regardless, "
        "so this only affects what's printed to the console",
    )
    parser.add_argument(
        "--speed-threshold-kn",
        type=float,
        default=0.5,
        help="Speed (kn) below this threshold counts as 'stationary' (default 0.5)",
    )
    parser.add_argument(
        "--min-stop-minutes",
        type=float,
        default=10.0,
        help="Minimum duration (minutes) stationary to count as a port visit (default 10)",
    )
    parser.add_argument(
        "--max-gap-minutes",
        type=float,
        default=None,
        help="From how many minutes without any data a trip gets cut short (default: same as "
        "--min-stop-minutes). Prevents a trip from bridging a large data gap (device/log was "
        "down) with a much-too-long reported duration.",
    )
    parser.add_argument(
        "--min-trip-distance-nm",
        type=float,
        default=0.1,
        help="A *finished* trip covering less than this is filtered out entirely as GPS/speed "
        "noise instead of shown as a (meaningless) logbook row (default 0.1 nm)",
    )
    parser.add_argument(
        "--min-leg-distance-nm",
        type=float,
        default=0.2,
        help="How short an in-transit leg between two stays can be before it's folded into its "
        "surrounding stay instead of ending one trip and starting another -- e.g. repositioning "
        "within the same harbour. Deliberately separate from and looser than "
        "--min-trip-distance-nm, which also decides whether an already-finished trip is real "
        "enough to appear in the logbook at all, so it has to stay tight. Set with a real "
        "tradeoff in mind, using the closest two real, confirmed anchors found so far: a genuine "
        "same-harbour final-approach hop (0.142 nm, Kerners -> Port du Crouesty) that must fold, "
        "and a genuine trip to a real, different nearby place (0.488 nm, Port Olona -> Les "
        "Sables-d'Olonne) that must not -- 0.2 nm sits closer to the fold case, on the owner's own "
        "judgment that Port du Crouesty's own harbour is sizeable enough that a value nearer to "
        "the middle of the gap felt too loose. A value picked without checking against real data "
        "caused a live-site incident before (0.5 nm silently merged that same real, different-"
        "place trip) -- verify any change to this default against a full season's worth of real "
        "trips, not just a handful of synthetic ones, before changing it again.",
    )
    parser.add_argument(
        "--lock-radius-m",
        type=float,
        default=10.0,
        help="A stop is treated as a lock/opening bridge (folded back into the trip instead of "
        "splitting it in two) if the engine was off no longer than --lock-max-duration-minutes "
        "and the boat stayed within this radius (meters) of its own position the whole time "
        "(default 10 m). Set to a negative value to disable this entirely.",
    )
    parser.add_argument(
        "--lock-max-duration-minutes",
        type=float,
        default=120.0,
        help="See --lock-radius-m: a confined stop where the engine was off longer than this "
        "counts as a real port visit regardless of how little it moved (default 120 minutes, "
        "i.e. 2 hours)",
    )
    parser.add_argument(
        "--no-geocode",
        action="store_true",
        help="Skip the online port-name lookup; shows coordinates instead of names",
    )
    parser.add_argument(
        "--no-sample-cache",
        action="store_true",
        help="Always re-parse every .ebl file from scratch, instead of reusing decoded samples "
        "cached from a previous run for files whose size hasn't changed since",
    )
    parser.add_argument(
        "--sample-cache-file",
        type=Path,
        default=Path(".ebl_sample_cache.pkl"),
        help="Cache directory for decoded .ebl samples, one small file per .ebl file "
        "(default .ebl_sample_cache.pkl; a legacy single-file cache at this path from an "
        "older version is migrated automatically)",
    )
    parser.add_argument(
        "--no-trip-cache",
        action="store_true",
        help="Always rebuild every trip from scratch, instead of reusing already-'settled' "
        "trips (everything before the last known trip's own departure) cached from a previous "
        "run -- see --trip-cache-file",
    )
    parser.add_argument(
        "--trip-cache-file",
        type=Path,
        default=Path(".trip_cache.pkl"),
        help="Cache file for already-settled trips, so a normal run only has to decode and "
        "rebuild the recent window since the last known trip's departure instead of the whole "
        "archive every time (default .trip_cache.pkl)",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=Path(".geocode_cache.json"),
        help="Cache file for port names (default .geocode_cache.json)",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="",
        help="Language for port names, as an Accept-Language code (e.g. 'nl', 'en'). Default: "
        "empty -- each place's own native/local name (whatever it's actually called there), not "
        "a fixed language picked ahead of time. See Geocoder's own 'language' doc comment in "
        "geocode.py for why.",
    )
    parser.add_argument(
        "--no-weather",
        action="store_true",
        help="Skip the online historical weather lookup; the log table's wind/precipitation/"
        "cloud cover columns stay empty",
    )
    parser.add_argument(
        "--weather-cache-file",
        type=Path,
        default=Path(".weather_cache.json"),
        help="Cache file for historical weather (default .weather_cache.json)",
    )
    parser.add_argument(
        "--no-marine",
        action="store_true",
        help="Skip the online historical wave/current lookup; the log table's wave/current "
        "columns stay empty",
    )
    parser.add_argument(
        "--marine-cache-file",
        type=Path,
        default=Path(".marine_cache.json"),
        help="Cache file for historical wave/current data (default .marine_cache.json)",
    )
    parser.add_argument(
        "--utc-offset",
        type=float,
        default=None,
        help="Fixed timezone offset in hours relative to UTC (e.g. 2 for CEST) for the displayed "
        "times. Default: automatically estimated per trip from the departure longitude (see README).",
    )
    parser.add_argument(
        "--boat-name",
        type=str,
        default=None,
        help="Boat name shown at the top of the HTML logbook (default: none, or the "
        "'boat_name' setting from the config file)",
    )
    parser.add_argument(
        "--mmsi",
        type=str,
        default=None,
        help="MMSI shown at the top of the HTML logbook (default: none, or the 'mmsi' setting "
        "from the config file)",
    )
    parser.add_argument(
        "--call-sign",
        type=str,
        default=None,
        help="Call sign shown at the top of the HTML logbook (default: none, or the "
        "'call_sign' setting from the config file)",
    )
    parser.add_argument(
        "--log-interval-minutes",
        type=float,
        default=_DEFAULT_LOG_INTERVAL_MINUTES,
        help="Interval (minutes) between the periodic course/speed/position entries in each "
        f"trip's 'Log' table in the HTML logbook (default {_DEFAULT_LOG_INTERVAL_MINUTES:g})",
    )
    parser.add_argument(
        "--remarks-api-url",
        type=str,
        default=_DEFAULT_REMARKS_API_URL,
        help="URL of a WordPress REST endpoint (see wordpress-plugin/) that stores per-trip "
        f"remarks, shown as a 'Remarks' button+popup per trip in the HTML logbook. Default: "
        f"'{_DEFAULT_REMARKS_API_URL}' -- a relative path resolves against whatever site the "
        f"logbook is opened from, so it works without also configuring a host as long as the "
        f"logbook is uploaded (see --upload) to the same site as the plugin. Set to an empty "
        f"string to disable.",
    )
    parser.add_argument(
        "--engine-count",
        type=int,
        default=None,
        help="Number of physical engines on the boat. With 1, any additional engine instance "
        "found in the data is treated as noise (a duplicate/ghost source) and discarded, and "
        "output drops the redundant 'engine 0:' label. Default: not set -- every distinct "
        "engine instance found in the data is kept and labeled (already unlabeled "
        "automatically if only one instance ever shows up).",
    )
    parser.add_argument(
        "--battery-warning-voltage",
        type=float,
        default=12.2,
        help="Flag a trip's battery voltage as low if it drops below this at any point (default "
        "12.2 V, a common 'getting low' threshold for a 12V lead-acid battery -- adjust for a "
        "24V system or a different battery chemistry). Shows up in the 'Warnings' column "
        "alongside engine warnings.",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload the HTML logbook over SFTP after writing it (see 'upload-host'/'upload-"
        "user'/'upload-remote-path'/'upload-key-file', or the matching [upload] settings in the "
        "config file)",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Force-disable --upload and --upload-rest for this run, overriding even a config "
        "file whose 'upload'/'upload_rest' settings are otherwise complete enough to enable them "
        "by default -- use this for a local test run so it can never touch the live site by "
        "accident (found in practice: a local test run with --no-geocode still uploaded, since "
        "the config file enables upload by default regardless of that flag)",
    )
    parser.add_argument(
        "--upload-host",
        type=str,
        default=None,
        help="SFTP host to upload the HTML logbook to (only with --upload)",
    )
    parser.add_argument(
        "--upload-user",
        type=str,
        default=None,
        help="SFTP username (only with --upload)",
    )
    parser.add_argument(
        "--upload-remote-path",
        type=str,
        default=None,
        help="Destination path on the SFTP server for the HTML logbook (only with --upload)",
    )
    parser.add_argument(
        "--upload-key-file",
        type=Path,
        default=None,
        help="Private SSH key file for the SFTP upload (only with --upload); the matching public "
        "key must be added to the server's SSH/SFTP access settings",
    )
    parser.add_argument(
        "--upload-port",
        type=int,
        default=22,
        help="SFTP port (default 22, only with --upload)",
    )
    parser.add_argument(
        "--upload-rest",
        action="store_true",
        help="Upload the HTML logbook to a WordPress REST endpoint after writing it, instead of "
        "over SFTP (see 'upload-rest-url'/'upload-rest-user'/'upload-rest-app-password', or the "
        "matching [upload] settings in the config file) -- takes priority over --upload when "
        "both are configured, since it needs no SSH key on this machine, just a WordPress "
        "Application Password. --no-upload disables this too.",
    )
    parser.add_argument(
        "--upload-rest-url",
        type=str,
        default=None,
        help="URL of the WordPress REST endpoint that receives the HTML logbook (see "
        "wordpress-plugin/nmea2log-remarks.php's /logbook route; only with --upload-rest)",
    )
    parser.add_argument(
        "--upload-rest-user",
        type=str,
        default=None,
        help="WordPress username for the REST upload (only with --upload-rest) -- needs the "
        "logboek_editor role (or Administrator) on the target site",
    )
    parser.add_argument(
        "--upload-rest-app-password",
        type=str,
        default=None,
        help="WordPress Application Password for the REST upload (only with --upload-rest) -- "
        "generated on the account's own profile page (Users > Profile > Application Passwords), "
        "not the account's real login password",
    )
    parser.add_argument(
        "--ebl-dir",
        type=Path,
        default=None,
        help="Folder to search recursively for .ebl files when no logfiles are given on the "
        "command line (e.g. the same folder nmea2log-download downloads into). Default: not "
        "set, or the 'ebl_dir' setting from the config file.",
    )
    _apply_config_defaults(parser)
    return parser


def _apply_config_defaults(parser: argparse.ArgumentParser) -> None:
    """Fills in argparse defaults from the ``[nmea2log]`` section of the config file (see
    ``config.py``; default ``nmea2log.ini`` in the current directory), so you don't have to
    pass the same options every time. Explicit command-line arguments always override this --
    argparse only applies a ``set_defaults`` value if the user didn't supply the option
    themselves."""
    section = load_section("nmea2log")
    if not section:
        return

    def _bool(value: str) -> bool:
        return value.strip().lower() in ("1", "true", "yes", "on")

    defaults: Dict[str, object] = {}
    for key, caster in (
        ("speed_threshold_kn", float),
        ("min_stop_minutes", float),
        ("max_gap_minutes", float),
        ("min_trip_distance_nm", float),
        ("min_leg_distance_nm", float),
        ("cache_file", Path),
        ("weather_cache_file", Path),
        ("marine_cache_file", Path),
        ("language", str),
        ("utc_offset", float),
        ("boat_name", str),
        ("mmsi", str),
        ("call_sign", str),
        ("log_interval_minutes", float),
        ("remarks_api_url", str),
        ("engine_count", int),
        ("battery_warning_voltage", float),
        ("ebl_dir", Path),
        ("lock_radius_m", float),
        ("lock_max_duration_minutes", float),
        ("sample_cache_file", Path),
        ("trip_cache_file", Path),
        ("log_retention_days", float),
    ):
        if key in section:
            defaults[key] = caster(section[key])
    if "no_geocode" in section:
        defaults["no_geocode"] = _bool(section["no_geocode"])
    if "no_trip_cache" in section:
        defaults["no_trip_cache"] = _bool(section["no_trip_cache"])
    if "no_weather" in section:
        defaults["no_weather"] = _bool(section["no_weather"])
    if "no_marine" in section:
        defaults["no_marine"] = _bool(section["no_marine"])
    if "no_sample_cache" in section:
        defaults["no_sample_cache"] = _bool(section["no_sample_cache"])
    if "csv" in section:
        defaults["csv"] = _bool(section["csv"])
    if "gpx" in section:
        defaults["gpx"] = _bool(section["gpx"])

    upload_section = load_section("upload")
    # Enabled by the four required settings themselves all being non-blank, not a separate
    # enabled=true/false to keep in sync with them -- matches the Android app's own settings
    # (SettingsStore.isSftpConfigComplete: host/user/password/remote_path all filled in, no
    # separate toggle) -- matches the Android app's own settings. --upload on the command line
    # (with the other --upload-* flags given directly, no config file involved) still works as
    # its own independent opt-in either way.
    if all(upload_section.get(key, "").strip() for key in ("host", "user", "remote_path", "key_file")):
        defaults["upload"] = True
    # Same pattern, for the REST-based logbook upload (see upload_via_rest() in upload.py) --
    # preferred over the SFTP settings above once configured (see _run()'s own choice between the
    # two), so publishing doesn't need a private SSH key sitting on every machine that syncs, just
    # a WordPress Application Password scoped to the logboek_editor role. hostname/username/
    # password rather than rest_url/rest_user/rest_app_password: generic REST-auth naming, not
    # tied to this being specifically a WordPress Application Password.
    if all(upload_section.get(key, "").strip() for key in ("hostname", "username", "password")):
        defaults["upload_rest"] = True
    for key, dest, caster in (
        ("host", "upload_host", str),
        ("user", "upload_user", str),
        ("remote_path", "upload_remote_path", str),
        ("key_file", "upload_key_file", Path),
        ("port", "upload_port", int),
        ("hostname", "upload_rest_url", str),
        ("username", "upload_rest_user", str),
        ("password", "upload_rest_app_password", str),
    ):
        if key in upload_section:
            defaults[dest] = caster(upload_section[key])

    parser.set_defaults(**defaults)


class _AlreadyRunningError(Exception):
    def __init__(self, pid: int) -> None:
        super().__init__(pid)
        self.pid = pid


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_lock(lock_path: Path) -> None:
    """Refuses to proceed if another nmea2log run against the same output directory is already
    in progress -- concurrent runs race on the shared sample cache file, the HTML output file,
    and (with --upload) the live site itself, and can silently produce a wrong result instead of
    a clear error (found in practice: re-launching nmea2log.bat because a slow run looked stuck
    raced with the still-running first instance, and the live site ended up showing only 1 of 15
    real trips). A leftover lock file from a run that crashed or was killed without cleaning up
    is detected by checking whether its PID is still alive, and silently taken over -- it's
    evidence of an abandoned run, not one still in progress.

    Uses an atomic exclusive-create (O_CREAT | O_EXCL) rather than a check-then-write, so two
    processes starting within the same instant can't both conclude the lock is free."""
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                other_pid = int(lock_path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                other_pid = None
            if other_pid is not None and _pid_is_running(other_pid):
                raise _AlreadyRunningError(other_pid)
            lock_path.unlink(missing_ok=True)  # stale -- clean up and retry
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return


def _release_lock(lock_path: Path) -> None:
    lock_path.unlink(missing_ok=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    lock_path = args.output.parent / ".nmea2log.lock"
    try:
        _acquire_lock(lock_path)
    except _AlreadyRunningError as exc:
        parser.error(
            f"another nmea2log run (pid {exc.pid}) is already in progress for "
            f"{args.output.parent} -- wait for it to finish before starting another one, or "
            f"delete {lock_path} if you're sure it isn't actually still running"
        )
    try:
        return _run(parser, args)
    finally:
        _release_lock(lock_path)


def _run(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    # Next to the output file, not the current directory -- so it's found in the same place
    # regardless of how nmea2log.bat happened to be launched. Console output alone disappears the
    # moment the terminal window closes, which for a run started by double-clicking a .bat file
    # leaves nothing to check afterwards, especially for a long one (found in practice).
    set_log_file(args.output.parent / "nmea2log.log", retention_days=args.log_retention_days)
    if args.verbose:
        set_log_level("debug")

    if args.no_upload:
        # Overrides even a config file whose 'upload'/'upload_rest' settings are complete enough
        # to enable them by default (see _apply_config_defaults) -- the whole point is a way to
        # run locally that's *guaranteed* not to touch the live site, regardless of what's already
        # sitting in nmea2log.ini (found in practice: forgetting that upload is enabled by default
        # there is exactly what caused a live-site incident twice).
        args.upload = False
        args.upload_rest = False

    if args.upload and not (args.upload_host and args.upload_user and args.upload_remote_path and args.upload_key_file):
        parser.error(
            "--upload needs --upload-host, --upload-user, --upload-remote-path, and "
            "--upload-key-file (or the matching settings in the [upload] section of the config "
            "file) to all be set"
        )
    if args.upload_rest and not (args.upload_rest_url and args.upload_rest_user and args.upload_rest_app_password):
        parser.error(
            "--upload-rest needs --upload-rest-url, --upload-rest-user, and "
            "--upload-rest-app-password (or the matching settings in the [upload] section of the "
            "config file) to all be set"
        )

    if not args.logfiles and args.ebl_dir:
        if not args.ebl_dir.is_dir():
            parser.error(f"--ebl-dir {args.ebl_dir} is not a directory")
        args.logfiles = _discover_ebl_files(args.ebl_dir)
        if not args.logfiles:
            parser.error(f"no .ebl files found under {args.ebl_dir}")
        log(f"[info] Found {len(args.logfiles)} .ebl file(s) under {args.ebl_dir}.", file=sys.stderr)

    if not args.logfiles:
        parser.error("provide one or more logfiles, or set ebl_dir in the config file")

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
    trip_cache_store = None if args.no_trip_cache else TripCache(args.trip_cache_file)
    settled_trips: list = []
    resume_index = 0  # first file index this run actually needs to decode -- 0 unless a usable
    # trip cache says otherwise, i.e. every file is decoded exactly like before trip caching existed
    resume_ebl_time_state_seed: object = None
    if trip_cache_store is not None:
        cached_trip_data = trip_cache_store.load(trip_signature)
        if cached_trip_data is not None:
            cached_settled_trips, resume_from_file, resume_ebl_time_state = cached_trip_data
            found_index = find_resume_index(args.logfiles, resume_from_file)
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
                    f"decode of {resume_index}/{len(args.logfiles)} file(s), resuming from "
                    f"{args.logfiles[resume_index].name}.",
                    file=sys.stderr,
                )

    sample_cache = None if args.no_sample_cache else SampleCache(args.sample_cache_file)
    geocoder = NoGeocoder() if args.no_geocode else Geocoder(cache_file=args.cache_file, language=args.language)
    weather = NoWeather() if args.no_weather else WeatherFetcher(cache_file=args.weather_cache_file)
    marine = NoMarine() if args.no_marine else MarineFetcher(cache_file=args.marine_cache_file)

    # Retried with resume_index widened by one file at a time -- see the check right after
    # build_trips() below -- if a resumed window turns out to have started mid-transit rather
    # than genuinely at the start of a stay. Everything inside this loop is scoped to a single
    # attempt and rebuilt from scratch each time; only settled_trips/resume_index/geocoder/
    # weather/marine/sample_cache carry over between attempts.
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
            ebl_time_state["current"] = resume_ebl_time_state_seed

        cache_hits = 0
        last_progress_log = time.monotonic()
        # Only populated for file indices actually decoded this attempt (>= resume_index) -- used
        # purely to pick where the *next* run should resume from, see choose_resume_index() in
        # trip_cache.py.
        file_first_time_by_index: Dict[int, Optional[datetime]] = {}
        ebl_time_state_before_file: Dict[int, object] = {}
        for idx, path in enumerate(args.logfiles, start=1):
            file_index = idx - 1
            if file_index < resume_index:
                # Already fully represented by settled_trips -- never even touched, not even to
                # check the sample cache, which is exactly the decode-time and decompression cost
                # this whole trip cache exists to avoid paying every single run.
                continue
            if not path.exists():
                log(f"[error] Log file not found: {path}", file=sys.stderr)
                return 1

            ebl_time_state_before_file[file_index] = ebl_time_state.get("current")

            cached = sample_cache.get(path) if sample_cache is not None else None
            if cached is not None:
                samples, time_state_after = cached
                fixes, sogs, engine, trip_fuel, depth, water_temp, battery, rpm, attitude = samples
                ebl_time_state["current"] = time_state_after
                cache_hits += 1
            else:
                frames = _iter_frames_for_path(path, ebl_time_state)
                samples = _collect_samples(frames)
                fixes, sogs, engine, trip_fuel, depth, water_temp, battery, rpm, attitude = samples
                if sample_cache is not None:
                    sample_cache.put(path, samples, ebl_time_state.get("current"))

            file_first_time: Optional[datetime] = None
            for source_fixes in fixes.values():
                if source_fixes:
                    candidate_time = min(f.time for f in source_fixes)
                    file_first_time = candidate_time if file_first_time is None else min(file_first_time, candidate_time)
            file_first_time_by_index[file_index] = file_first_time

            now = time.monotonic()
            if now - last_progress_log >= _DECODE_PROGRESS_INTERVAL_S and idx < len(args.logfiles):
                log(f"[info] ...decoded {idx}/{len(args.logfiles)} logfile(s) so far", file=sys.stderr)
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
        log(f"[info] ...decoded {len(args.logfiles)}/{len(args.logfiles)} logfile(s) so far", file=sys.stderr)

        decoded_file_count = len(args.logfiles) - resume_index
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

        if not all_fixes and not settled_trips:
            log("[error] No position data (PGN 129025) found.", file=sys.stderr)
            return 1

        if args.engine_count == 1:
            all_engine, all_trip_fuel, all_rpm = _filter_to_dominant_engine(all_engine, all_trip_fuel, all_rpm)

        if all_fixes:
            # Found in practice: build_trips() (and write_html_logbook() below) can run for a real
            # stretch of time on a full multi-year archive (millions of merged GPS fixes) with zero
            # log output in between -- decode's own progress logging (see
            # _DECODE_PROGRESS_INTERVAL_S) stops the moment the last file is read, leaving nothing on
            # screen to distinguish "still working" from "hung" or "already crashed silently" for
            # however long this phase takes.
            log(f"[info] Reizen opbouwen uit {len(all_fixes)} GPS-posities...", file=sys.stderr)
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
                f"trip) -- widening by one file ({args.logfiles[resume_index].name}) and "
                f"retrying (attempt {widen_attempts}/{_MAX_RESUME_WIDEN_ATTEMPTS}).",
                file=sys.stderr,
            )
            continue
        break

    trips = settled_trips + fresh_trips

    if not trips:
        log(
            "[error] No trips found (maybe never stopped or underway long enough relative to the thresholds).",
            file=sys.stderr,
        )
        return 1

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
        new_resume_file = str(args.logfiles[new_resume_index].resolve())
        new_resume_state = ebl_time_state_before_file.get(new_resume_index)
        trip_cache_store.save(new_settled_trips, new_resume_file, new_resume_state, trip_signature)

    # Every run, over every trip -- settled (just loaded straight from trip_cache_store.load()
    # above, never touched by build_trips() at all this run) included, not just the fresh ones --
    # see resolve_trip_places()'s own doc comment for why this can't just happen once inside
    # build_trips() and get cached alongside everything else about a settled trip.
    trips = resolve_trip_places(trips, geocoder)

    log(f"[info] {len(trips)} trip(s) found, writing logbook...", file=sys.stderr)
    trip_uids = assign_trip_ids(trips, utc_offset_hours=args.utc_offset)

    # When the logbook is actually about to be written, not the latest timestamp found in the
    # data (an earlier version used the latter -- PGN 126992's last known time -- but that made
    # "Laatst bijgewerkt" ambiguous: it looked unchanged after a fresh run whenever the boat
    # itself hadn't produced new data since the previous run) and not right after decode either
    # (an even earlier version of *this* fix used that instead -- found in practice, on a real
    # slow run: geocoding/build_trips() can take several extra minutes after decode finishes,
    # e.g. waiting out Overpass rate-limit retries, so a decode-time stamp could sit visibly
    # behind the moment the page was actually produced, reading as if the file were already
    # stale the instant it went up). This is what's actually meant to answer: "is this page
    # showing a stale file".
    latest_data_at = datetime.now(timezone.utc).replace(tzinfo=None)

    if args.csv:
        write_csv(
            trips, args.output, utc_offset_hours=args.utc_offset, battery_warning_voltage=args.battery_warning_voltage
        )
        log(f"[ok] Logbook written: {args.output}")
    if args.gpx:
        gpx_path = args.output.with_suffix(".gpx")
        write_gpx(
            trips, gpx_path, utc_offset_hours=args.utc_offset, battery_warning_voltage=args.battery_warning_voltage
        )
        log(f"[ok] Route written: {gpx_path}")
    html_path = args.output.with_suffix(".html")
    write_html_logbook(
        trips,
        html_path,
        boat_name=args.boat_name,
        mmsi=args.mmsi,
        call_sign=args.call_sign,
        utc_offset_hours=args.utc_offset,
        trip_uids=trip_uids,
        battery_warning_voltage=args.battery_warning_voltage,
        latest_data_at=latest_data_at,
        log_interval_minutes=args.log_interval_minutes,
        remarks_api_url=args.remarks_api_url,
        weather=weather,
        marine=marine,
        geocoder=geocoder,
        latest_position=all_fixes[-1] if all_fixes else None,
    )
    log(f"[ok] HTML logbook written: {html_path} ({len(trips)} trip(s))")

    # REST takes priority over SFTP when both happen to be configured -- see --upload-rest's own
    # help text for why (no SSH key needed on this machine). Not "REST, or SFTP as a fallback if
    # REST fails" within the same run: a failed upload should surface as a failed upload, not
    # silently retry a completely different transport the operator may not have intended to lean
    # on at all.
    if args.upload_rest:
        try:
            upload_via_rest(
                html_path.read_bytes(),
                url=args.upload_rest_url,
                user=args.upload_rest_user,
                app_password=args.upload_rest_app_password,
            )
            log(f"[ok] Uploaded via plugin to {args.upload_rest_url}")
        except UploadError as exc:
            log(f"[error] upload failed:\n{exc}", file=sys.stderr)
            return 1
    elif args.upload:
        try:
            upload_file(
                html_path,
                host=args.upload_host,
                user=args.upload_user,
                remote_path=args.upload_remote_path,
                key_file=args.upload_key_file,
                port=args.upload_port,
            )
            log(f"[ok] Uploaded via SFTP to {args.upload_user}@{args.upload_host}:{args.upload_remote_path}")
        except UploadError as exc:
            # A newline after "failed:", not a space -- the SFTP client's own error message can
            # itself be multi-line (e.g. the server's login banner), which otherwise starts
            # awkwardly mid-line right after the prefix (found in practice).
            log(f"[error] upload failed:\n{exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
