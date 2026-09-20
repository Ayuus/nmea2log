from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .config import load_section
from .geocode import Geocoder, NoGeocoder
from .gpx_writer import write_gpx
from .html_writer import _DEFAULT_LOG_INTERVAL_MINUTES, _DEFAULT_REMARKS_API_URL, write_html_logbook
from .log import DEFAULT_LOG_RETENTION_DAYS, log, set_log_file, set_log_level
from .logbook_writer import write_csv
from .marine import MarineFetcher, NoMarine
from .pipeline import PipelineError, _discover_ebl_files, build_season_trips
from .trip_ids import assign_trip_ids
from .upload import UploadError, upload_file, upload_via_rest
from .weather import NoWeather, WeatherFetcher


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nmea2log",
        description="Turn NMEA2000 log files from an Actisense W2K-2 into a sailing logbook (CSV).",
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
        help="Folder to search recursively for .ebl files (e.g. the same folder nmea2log-download "
        "downloads into). Default: not set, or the 'ebl_dir' setting from the config file.",
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

    if not args.ebl_dir:
        parser.error("--ebl-dir is required (or set ebl_dir in the config file)")
    if not args.ebl_dir.is_dir():
        parser.error(f"--ebl-dir {args.ebl_dir} is not a directory")
    # Already sorted (see _discover_ebl_files's own docstring) -- the resume-cache logic below
    # (resume_index as a plain cutoff into this list) and every season-wide sample array
    # build_trips() accumulates (fixes/sogs/attitude/...) both assume file order corresponds to
    # chronological order -- see android_entry.py's own run_pipeline() for the real-world version
    # of this same assumption breaking (Kotlin's file listing has no ordering guarantee there).
    args.logfiles = _discover_ebl_files(args.ebl_dir)
    if not args.logfiles:
        parser.error(f"no .ebl files found under {args.ebl_dir}")
    log(f"[info] Found {len(args.logfiles)} .ebl file(s) under {args.ebl_dir}.", file=sys.stderr)

    geocoder = NoGeocoder() if args.no_geocode else Geocoder(cache_file=args.cache_file, language=args.language)
    weather = NoWeather() if args.no_weather else WeatherFetcher(cache_file=args.weather_cache_file)
    marine = NoMarine() if args.no_marine else MarineFetcher(cache_file=args.marine_cache_file)

    try:
        season = build_season_trips(
            args.logfiles,
            args,
            sample_cache_path=None if args.no_sample_cache else args.sample_cache_file,
            trip_cache_path=None if args.no_trip_cache else args.trip_cache_file,
            geocoder=geocoder,
        )
    except PipelineError as exc:
        log(f"[error] {exc}", file=sys.stderr)
        return 1
    trips = season.trips

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
        latest_position=season.latest_position,
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
