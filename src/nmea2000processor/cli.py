from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, TypeVar

from .config import load_section
from .ebl_reader import iter_frames as iter_frames_ebl
from .geocode import Geocoder, NoGeocoder
from .weather import NoWeather, WeatherFetcher
from .gpx_writer import write_gpx
from .html_writer import _DEFAULT_LOG_INTERVAL_MINUTES, _DEFAULT_REMARKS_API_URL, write_html_logbook
from .log import DEFAULT_LOG_RETENTION_DAYS, log, set_log_file
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
from .tripbuilder import build_trips
from .upload import UploadError, list_remote_filenames, upload_file, upload_files

_T = TypeVar("_T")

# How many logfiles --backup-ebl uploads per SFTP session, rather than all of them (potentially
# 1000+, several MB each on a first-ever backup run) in one -- found in practice: a shared-hosting
# server reset the connection partway through a single giant session (12 files/~60 MB in),
# discarding whatever hadn't already landed. Smaller sessions mean a reset loses less progress at
# once, and there's nothing to gain from one huge session anyway (list_remote_filenames already
# makes the whole thing resumable across runs; nothing here depends on one session covering
# everything).
_BACKUP_CHUNK_SIZE = 25

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


def _filter_to_dominant_engine(
    engine_samples: List[EngineSample],
    trip_fuel_samples: List[TripFuelSample],
    rpm_samples: List[EngineRpmSample],
) -> Tuple[List[EngineSample], List[TripFuelSample], List[EngineRpmSample]]:
    """Keeps only the engine instance with the most samples, discarding any other instance
    entirely. Used when ``--engine-count 1`` tells us there's really just one physical engine,
    so any additional instance that shows up in the data is noise (a duplicate/ghost source),
    not a second engine -- the same idea as ``_dominant_source_only`` for GPS sources."""
    by_instance: Dict[int, List[EngineSample]] = {}
    for sample in engine_samples:
        by_instance.setdefault(sample.instance, []).append(sample)
    if len(by_instance) <= 1:
        return engine_samples, trip_fuel_samples, rpm_samples

    dominant = max(by_instance, key=lambda instance: len(by_instance[instance]))
    filtered_fuel = [sample for sample in trip_fuel_samples if sample.instance == dominant]
    filtered_rpm = [sample for sample in rpm_samples if sample.instance == dominant]
    return by_instance[dominant], filtered_fuel, filtered_rpm


def _select_primary_gps_source(
    fixes_by_source: Dict[int, List[PositionFix]], sogs_by_source: Dict[int, List[SogSample]]
) -> Tuple[List[PositionFix], List[SogSample], Optional[int]]:
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
        return [], _dominant_source_only(sogs_by_source), None

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
        help="Trips covering less than this are filtered out as GPS/speed noise instead of "
        "shown as a (meaningless) logbook row (default 0.1 nm)",
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
        help="Cache file for decoded .ebl samples (default .ebl_sample_cache.pkl)",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=Path(".geocode_cache.json"),
        help="Cache file for port names (default .geocode_cache.json)",
    )
    parser.add_argument("--language", type=str, default="nl", help="Language for port names (default nl)")
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
        "--download-failed",
        action="store_true",
        help="Marks the 'Laatst bijgewerkt' timestamp in the HTML logbook in red -- pass this "
        "when a preceding download step (e.g. nmea2log-download) failed, so it's visible at a "
        "glance that this run couldn't fetch any new data and just regenerated the file from "
        "what was already cached (set automatically by nmea2log.bat)",
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
        "remarks, shown as a 'Remarks' button+popup per trip in the HTML logbook. Default: "
        "disabled (empty). Typically '/wp-json/nmea2log/v1/remarks' -- a relative path resolves "
        "against whatever site the logbook is opened from, so it works without also configuring "
        "a host as long as the logbook is uploaded (see --upload) to the same site as the plugin.",
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
        help="Force-disable both --upload and --backup-ebl for this run, overriding even the "
        "config file's own 'enabled'/'backup_ebl' settings -- use this for a local test run so it "
        "can never touch the live site by accident (found in practice: a local test run with "
        "--no-geocode still uploaded, since the config file enables upload by default regardless "
        "of that flag)",
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
        "--backup-ebl",
        action="store_true",
        help="Back up this run's own logfiles (e.g. .ebl files) to the SFTP server too, so "
        "they're not only sitting on whatever device downloaded them (see 'backup-remote-path', "
        "or the matching [upload] settings in the config file; reuses --upload-host/-user/"
        "-key-file/-port). Skips files already present on the server, so only ever uploads "
        "what's new since the last run.",
    )
    parser.add_argument(
        "--backup-remote-path",
        type=str,
        default=None,
        help="Destination directory on the SFTP server for the logfile backup (only with "
        "--backup-ebl)",
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
        ("cache_file", Path),
        ("weather_cache_file", Path),
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
        ("log_retention_days", float),
    ):
        if key in section:
            defaults[key] = caster(section[key])
    if "no_geocode" in section:
        defaults["no_geocode"] = _bool(section["no_geocode"])
    if "no_weather" in section:
        defaults["no_weather"] = _bool(section["no_weather"])
    if "no_sample_cache" in section:
        defaults["no_sample_cache"] = _bool(section["no_sample_cache"])
    if "csv" in section:
        defaults["csv"] = _bool(section["csv"])
    if "gpx" in section:
        defaults["gpx"] = _bool(section["gpx"])

    upload_section = load_section("upload")
    if "enabled" in upload_section:
        defaults["upload"] = _bool(upload_section["enabled"])
    if "backup_ebl" in upload_section:
        defaults["backup_ebl"] = _bool(upload_section["backup_ebl"])
    for key, dest, caster in (
        ("host", "upload_host", str),
        ("user", "upload_user", str),
        ("remote_path", "upload_remote_path", str),
        ("key_file", "upload_key_file", Path),
        ("port", "upload_port", int),
        ("backup_remote_path", "backup_remote_path", str),
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
    # leaves nothing to check afterwards -- particularly for a --backup-ebl run that can take a
    # while and is easy to interrupt by closing the window too early (found in practice).
    set_log_file(args.output.parent / "nmea2log.log", retention_days=args.log_retention_days)

    if args.no_upload:
        # Overrides even a config-file default of enabled=true/backup_ebl=true -- the whole point
        # is a way to run locally that's *guaranteed* not to touch the live site, regardless of
        # what's already sitting in nmea2log.ini (found in practice: forgetting that upload is
        # enabled by default there is exactly what caused a live-site incident twice).
        args.upload = False
        args.backup_ebl = False

    if args.upload and not (args.upload_host and args.upload_user and args.upload_remote_path and args.upload_key_file):
        parser.error(
            "--upload needs --upload-host, --upload-user, --upload-remote-path, and "
            "--upload-key-file (or the matching settings in the [upload] section of the config "
            "file) to all be set"
        )
    if args.backup_ebl and not (args.upload_host and args.upload_user and args.upload_key_file and args.backup_remote_path):
        parser.error(
            "--backup-ebl needs --upload-host, --upload-user, --upload-key-file, and "
            "--backup-remote-path (or the matching settings in the [upload] section of the "
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

    fixes_by_source: Dict[int, List[PositionFix]] = {}
    sogs_by_source: Dict[int, List[SogSample]] = {}
    depth_by_source: Dict[int, List[DepthSample]] = {}
    water_temp_by_source: Dict[int, List[WaterTempSample]] = {}
    battery_by_source: Dict[int, List[BatterySample]] = {}
    attitude_by_source: Dict[int, List[AttitudeSample]] = {}
    all_engine: List[EngineSample] = []
    all_trip_fuel: List[TripFuelSample] = []
    all_rpm: List[EngineRpmSample] = []

    ebl_time_state: Dict[str, object] = {}
    sample_cache = None if args.no_sample_cache else SampleCache(args.sample_cache_file)
    cache_hits = 0
    for path in args.logfiles:
        if not path.exists():
            log(f"[error] Log file not found: {path}", file=sys.stderr)
            return 1

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

        _merge_by_source(fixes_by_source, fixes)
        _merge_by_source(sogs_by_source, sogs)
        all_engine += engine
        all_trip_fuel += trip_fuel
        _merge_by_source(depth_by_source, depth)
        _merge_by_source(water_temp_by_source, water_temp)
        _merge_by_source(battery_by_source, battery)
        all_rpm += rpm
        _merge_by_source(attitude_by_source, attitude)

    if sample_cache is not None:
        sample_cache.save()
        if cache_hits:
            log(
                f"[cache] reused decoded samples for {cache_hits}/{len(args.logfiles)} file(s), "
                f"only re-parsed {len(args.logfiles) - cache_hits}",
                file=sys.stderr,
            )

    # When this run actually happened, not the latest timestamp found in the data -- the earlier
    # version used the latter (PGN 126992's last known time), but that made "Laatst bijgewerkt"
    # ambiguous: it looked unchanged after a fresh run whenever the boat itself hadn't produced
    # new data since the previous run, when what it's actually meant to answer is "is this page
    # showing a stale file" (found in practice, asked for explicitly).
    latest_data_at = datetime.now(timezone.utc).replace(tzinfo=None)

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

    if not all_fixes:
        log("[error] No position data (PGN 129025) found.", file=sys.stderr)
        return 1

    if args.engine_count == 1:
        all_engine, all_trip_fuel, all_rpm = _filter_to_dominant_engine(all_engine, all_trip_fuel, all_rpm)

    geocoder = NoGeocoder() if args.no_geocode else Geocoder(cache_file=args.cache_file, language=args.language)
    weather = NoWeather() if args.no_weather else WeatherFetcher(cache_file=args.weather_cache_file)

    trips = build_trips(
        all_fixes,
        all_sogs,
        all_engine,
        all_trip_fuel,
        all_depth,
        all_water_temp,
        all_battery,
        all_rpm,
        all_attitude,
        geocoder=geocoder,
        speed_threshold_kn=args.speed_threshold_kn,
        min_stop_minutes=args.min_stop_minutes,
        max_gap_minutes=args.max_gap_minutes,
        min_trip_distance_nm=args.min_trip_distance_nm,
        lock_radius_m=args.lock_radius_m if args.lock_radius_m >= 0 else None,
        lock_max_duration_minutes=args.lock_max_duration_minutes,
    )

    if not trips:
        log(
            "[error] No trips found (maybe never stopped or underway long enough relative to the thresholds).",
            file=sys.stderr,
        )
        return 1

    trip_uids = assign_trip_ids(trips, utc_offset_hours=args.utc_offset)

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
        fetch_failed=args.download_failed,
        log_interval_minutes=args.log_interval_minutes,
        remarks_api_url=args.remarks_api_url,
        weather=weather,
    )
    log(f"[ok] HTML logbook written: {html_path} ({len(trips)} trip(s))")

    if args.upload:
        try:
            upload_file(
                html_path,
                host=args.upload_host,
                user=args.upload_user,
                remote_path=args.upload_remote_path,
                key_file=args.upload_key_file,
                port=args.upload_port,
            )
            log(f"[ok] Uploaded to {args.upload_user}@{args.upload_host}:{args.upload_remote_path}")
        except UploadError as exc:
            # A newline after "failed:", not a space -- the SFTP client's own error message can
            # itself be multi-line (e.g. the server's login banner), which otherwise starts
            # awkwardly mid-line right after the prefix (found in practice).
            log(f"[error] upload failed:\n{exc}", file=sys.stderr)
            return 1

    if args.backup_ebl:
        # A failed (or partial) backup is deliberately never fatal to the run -- it's a bonus
        # resilience step on top of already having written and uploaded today's logbook, not
        # something today's run actually depends on, and it's fully resumable: whatever made it
        # to the server this time is skipped next time (see list_remote_filenames), so an
        # unfinished backup just continues from there rather than needing to be retried whole.
        backed_up_count = 0  # set before the try so the except below can always reference it
        try:
            # Only ever the logfiles this run actually processed.
            already_backed_up = list_remote_filenames(
                host=args.upload_host,
                user=args.upload_user,
                remote_dir=args.backup_remote_path,
                key_file=args.upload_key_file,
                port=args.upload_port,
            )
            new_files = [path for path in args.logfiles if path.name not in already_backed_up]
            for start in range(0, len(new_files), _BACKUP_CHUNK_SIZE):
                chunk = new_files[start : start + _BACKUP_CHUNK_SIZE]
                upload_files(
                    chunk,
                    host=args.upload_host,
                    user=args.upload_user,
                    remote_dir=args.backup_remote_path,
                    key_file=args.upload_key_file,
                    port=args.upload_port,
                )
                backed_up_count += len(chunk)
                # A backup of hundreds/thousands of files can genuinely take a while; without any
                # feedback in between, it can look stuck and invite closing the terminal early --
                # which kills the whole (still-blocking) run, backup included (found in practice).
                if len(new_files) > _BACKUP_CHUNK_SIZE:
                    log(f"[info] ...backed up {backed_up_count}/{len(new_files)} new logfile(s) so far")
            log(
                f"[ok] Backed up {backed_up_count} new logfile(s) to "
                f"{args.upload_user}@{args.upload_host}:{args.backup_remote_path} "
                f"({len(args.logfiles) - len(new_files)} already there)"
            )
        except UploadError as exc:
            log(
                f"[warning] logfile backup stopped after {backed_up_count} new file(s) this run "
                f"(will pick up from there next time):\n{exc}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
