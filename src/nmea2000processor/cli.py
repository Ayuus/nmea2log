from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, TypeVar

from .ascii_reader import iter_frames
from .config import load_section
from .ebl_reader import iter_frames as iter_frames_ebl
from .geocode import Geocoder, NoGeocoder
from .gpx_writer import write_gpx
from .html_writer import write_html_logbook
from .logbook_writer import write_csv
from .model import DepthSample, EngineSample, Frame, PositionFix, SogSample, TripFuelSample
from .network_reader import DEFAULT_PORT, iter_frames_tcp
from .pgn_decode import (
    PGN_COG_SOG_RAPID,
    PGN_ENGINE_DYNAMIC,
    PGN_POSITION_RAPID,
    PGN_TRIP_FUEL_ENGINE,
    PGN_WATER_DEPTH,
    decode_engine_dynamic,
    decode_position_rapid,
    decode_sog,
    decode_trip_fuel_engine,
    decode_water_depth,
)
from .trip_ids import assign_trip_ids
from .tripbuilder import build_trips

_T = TypeVar("_T")


def _dominant_source_only(by_source: Dict[int, List[_T]]) -> List[_T]:
    """Some boats have multiple devices sending the same PGN (e.g. two GPS antennas that both
    send position or speed over ground). Without filtering, their independent, slightly
    differing readings get interleaved purely by time, which causes hundreds of small false
    "jumps" that together can significantly inflate the distance. We therefore only keep the
    source that sent the most messages -- across the whole session (all files/the whole live
    connection) together, not per file, otherwise a different source could "win" in each file
    and the problem would just come back at the seam between files."""
    if not by_source:
        return []
    dominant_source = max(by_source, key=lambda source: len(by_source[source]))
    return by_source[dominant_source]


def _merge_by_source(target: Dict[int, List[_T]], addition: Dict[int, List[_T]]) -> None:
    for source, items in addition.items():
        target.setdefault(source, []).extend(items)


def _filter_to_dominant_engine(
    engine_samples: List[EngineSample], trip_fuel_samples: List[TripFuelSample]
) -> Tuple[List[EngineSample], List[TripFuelSample]]:
    """Keeps only the engine instance with the most samples, discarding any other instance
    entirely. Used when ``--engine-count 1`` tells us there's really just one physical engine,
    so any additional instance that shows up in the data is noise (a duplicate/ghost source),
    not a second engine -- the same idea as ``_dominant_source_only`` for GPS sources."""
    by_instance: Dict[int, List[EngineSample]] = {}
    for sample in engine_samples:
        by_instance.setdefault(sample.instance, []).append(sample)
    if len(by_instance) <= 1:
        return engine_samples, trip_fuel_samples

    dominant = max(by_instance, key=lambda instance: len(by_instance[instance]))
    filtered_fuel = [sample for sample in trip_fuel_samples if sample.instance == dominant]
    return by_instance[dominant], filtered_fuel


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
    frames: Iterable[Frame], *, deadline: Optional[float] = None
) -> Tuple[
    Dict[int, List[PositionFix]],
    Dict[int, List[SogSample]],
    List[EngineSample],
    List[TripFuelSample],
    Dict[int, List[DepthSample]],
]:
    """Processes frames into samples, grouped by source address for PGNs that can come from
    multiple devices at once. Stops cleanly on Ctrl+C or once the deadline passes, so a live
    session always produces a logbook of whatever came in up to that point."""
    fixes_by_source: Dict[int, List[PositionFix]] = {}
    sogs_by_source: Dict[int, List[SogSample]] = {}
    depth_by_source: Dict[int, List[DepthSample]] = {}
    engine_samples: List[EngineSample] = []
    trip_fuel_samples: List[TripFuelSample] = []
    try:
        for frame in frames:
            if deadline is not None and time.monotonic() >= deadline:
                print("Duration elapsed; closing live session...", file=sys.stderr)
                break
            if frame.pgn == PGN_POSITION_RAPID:
                decoded = decode_position_rapid(frame.data)
                if decoded is not None:
                    lat, lon = decoded
                    fixes_by_source.setdefault(frame.source, []).append(PositionFix(frame.time, lat, lon))
            elif frame.pgn == PGN_COG_SOG_RAPID:
                sog = decode_sog(frame.data)
                if sog is not None:
                    sogs_by_source.setdefault(frame.source, []).append(SogSample(frame.time, sog))
            elif frame.pgn == PGN_ENGINE_DYNAMIC:
                decoded = decode_engine_dynamic(frame.data)
                if decoded is not None:
                    engine_samples.append(EngineSample(time=frame.time, **decoded))
            elif frame.pgn == PGN_TRIP_FUEL_ENGINE:
                decoded = decode_trip_fuel_engine(frame.data)
                if decoded is not None:
                    instance, trip_fuel_l = decoded
                    trip_fuel_samples.append(TripFuelSample(frame.time, instance, trip_fuel_l))
            elif frame.pgn == PGN_WATER_DEPTH:
                depth_m = decode_water_depth(frame.data)
                if depth_m is not None:
                    depth_by_source.setdefault(frame.source, []).append(DepthSample(frame.time, depth_m))
    except KeyboardInterrupt:
        print("\nInterrupted by user; writing the logbook with the data collected so far...", file=sys.stderr)
    return fixes_by_source, sogs_by_source, engine_samples, trip_fuel_samples, depth_by_source


def _iter_frames_for_path(
    path: Path, start_date: Optional[date], ebl_time_state: Optional[Dict[str, object]] = None
) -> Iterable[Frame]:
    """Picks the right parser based on the file extension: .ebl -> binary SD card log,
    everything else -> N2K ASCII (live TCP stream captured to a file, see --tee).

    ``ebl_time_state`` is passed to consecutive .ebl files so the last known time (PGN 126992)
    is preserved across file boundaries -- otherwise a file without its own System Time message
    (e.g. anchored for a long time, GPS/plotter idle) gets discarded entirely, even though the
    time is already known from the previous file (see ebl_reader.py)."""
    if path.suffix.lower() == ".ebl":
        return iter_frames_ebl(path, time_state=ebl_time_state)
    return iter_frames(path, start_date=start_date)


def _parse_host_port(value: str, default_port: int) -> Tuple[str, int]:
    if ":" in value:
        host, _, port_str = value.rpartition(":")
        return host, int(port_str)
    return value, default_port


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nmea2log",
        description="Turn NMEA2000 data from an Actisense W2K-2 (N2K ASCII) into a sailing logbook (CSV), "
        "from stored log files or live over a TCP connection.",
    )
    parser.add_argument(
        "logfiles",
        nargs="*",
        type=Path,
        help="One or more log files: .ebl (SD card log from the W2K-2) or .raw/.n2k (N2K ASCII, "
        "e.g. captured via --live --tee). Do not combine with --live.",
    )
    parser.add_argument(
        "--live",
        metavar="HOST[:PORT]",
        default=None,
        help=f"Connect live to the W2K-2 over TCP (e.g. 192.168.4.1 or 192.168.4.1:60001; "
        f"default port {DEFAULT_PORT}). Runs until Ctrl+C or --duration elapses.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Only with --live: stop automatically after this many seconds",
    )
    parser.add_argument(
        "--tee",
        type=Path,
        default=None,
        help="Only with --live: also write the raw incoming ASCII lines to this file "
        "(appending), so alongside live processing you also end up with a log file",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("logbook.csv"), help="Path to the CSV file (default: logbook.csv)"
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Start date YYYY-MM-DD for the first log file (otherwise guessed from the file name or "
        "modification date). Not applicable with --live or .ebl files (those get their date/time from the data itself).",
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
        "--no-geocode",
        action="store_true",
        help="Skip the online port-name lookup; shows coordinates instead of names",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=Path(".geocode_cache.json"),
        help="Cache file for port names (default .geocode_cache.json)",
    )
    parser.add_argument("--language", type=str, default="nl", help="Language for port names (default nl)")
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
        "--engine-count",
        type=int,
        default=None,
        help="Number of physical engines on the boat. With 1, any additional engine instance "
        "found in the data is treated as noise (a duplicate/ghost source) and discarded, and "
        "output drops the redundant 'engine 0:' label. Default: not set -- every distinct "
        "engine instance found in the data is kept and labeled (already unlabeled "
        "automatically if only one instance ever shows up).",
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
        ("language", str),
        ("utc_offset", float),
        ("boat_name", str),
        ("engine_count", int),
    ):
        if key in section:
            defaults[key] = caster(section[key])
    if "no_geocode" in section:
        defaults["no_geocode"] = _bool(section["no_geocode"])

    parser.set_defaults(**defaults)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if bool(args.logfiles) == bool(args.live):
        parser.error("provide either one or more logfiles or --live HOST[:PORT] (not both, not neither)")
    if args.duration is not None and not args.live:
        parser.error("--duration only applies together with --live")
    if args.tee is not None and not args.live:
        parser.error("--tee only applies together with --live")

    fixes_by_source: Dict[int, List[PositionFix]] = {}
    sogs_by_source: Dict[int, List[SogSample]] = {}
    depth_by_source: Dict[int, List[DepthSample]] = {}
    all_engine: List[EngineSample] = []
    all_trip_fuel: List[TripFuelSample] = []

    if args.live:
        host, port = _parse_host_port(args.live, DEFAULT_PORT)
        print(f"Connecting live to {host}:{port}... (Ctrl+C to stop)", file=sys.stderr)
        try:
            frames = iter_frames_tcp(host, port, tee_to=args.tee)
        except OSError as exc:
            print(f"Could not connect to {host}:{port}: {exc}", file=sys.stderr)
            return 1
        deadline = time.monotonic() + args.duration if args.duration else None
        fixes_by_source, sogs_by_source, all_engine, all_trip_fuel, depth_by_source = _collect_samples(
            frames, deadline=deadline
        )
    else:
        start_date = date.fromisoformat(args.start_date) if args.start_date else None
        ebl_time_state: Dict[str, object] = {}
        for index, path in enumerate(args.logfiles):
            if not path.exists():
                print(f"Log file not found: {path}", file=sys.stderr)
                return 1
            frames = _iter_frames_for_path(path, start_date if index == 0 else None, ebl_time_state)
            fixes, sogs, engine, trip_fuel, depth = _collect_samples(frames)
            _merge_by_source(fixes_by_source, fixes)
            _merge_by_source(sogs_by_source, sogs)
            all_engine += engine
            all_trip_fuel += trip_fuel
            _merge_by_source(depth_by_source, depth)

    all_fixes, all_sogs, primary_gps_source = _select_primary_gps_source(fixes_by_source, sogs_by_source)
    all_depth = _dominant_source_only(depth_by_source)

    if len(fixes_by_source) > 1:
        print(
            f"Multiple position sources found ({sorted(fixes_by_source)}); "
            f"using source {primary_gps_source} as the primary GPS (most messages).",
            file=sys.stderr,
        )

    if not all_fixes:
        print("No position data (PGN 129025) found.", file=sys.stderr)
        return 1

    if args.engine_count == 1:
        all_engine, all_trip_fuel = _filter_to_dominant_engine(all_engine, all_trip_fuel)

    geocoder = NoGeocoder() if args.no_geocode else Geocoder(cache_file=args.cache_file, language=args.language)

    trips = build_trips(
        all_fixes,
        all_sogs,
        all_engine,
        all_trip_fuel,
        all_depth,
        geocoder=geocoder,
        speed_threshold_kn=args.speed_threshold_kn,
        min_stop_minutes=args.min_stop_minutes,
        max_gap_minutes=args.max_gap_minutes,
        min_trip_distance_nm=args.min_trip_distance_nm,
    )

    if not trips:
        print(
            "No trips found (maybe never stopped or underway long enough relative to the thresholds).",
            file=sys.stderr,
        )
        return 1

    trip_uids = assign_trip_ids(trips, utc_offset_hours=args.utc_offset)

    write_csv(trips, args.output, utc_offset_hours=args.utc_offset)
    gpx_path = args.output.with_suffix(".gpx")
    write_gpx(trips, gpx_path, utc_offset_hours=args.utc_offset)
    html_path = args.output.with_suffix(".html")
    write_html_logbook(
        trips, html_path, boat_name=args.boat_name, utc_offset_hours=args.utc_offset, trip_uids=trip_uids
    )
    print(f"Logbook written: {args.output} ({len(trips)} trip(s))")
    print(f"Route written: {gpx_path}")
    print(f"HTML logbook written: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
