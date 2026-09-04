"""Android entry point: the same decode -> build trips -> write HTML pipeline as cli.py's
_run(), minus argparse and minus upload. Called from Kotlin via Chaquopy with a list of already-
downloaded .ebl files and settings read from EncryptedSharedPreferences, instead of reading
nmea2log.ini or parsing command-line flags.

run_pipeline() returns a plain dict instead of an exit code / raising SystemExit -- a background
sync (or the manual "Sync now" test button) can't rely on stderr text or a process return code
the way the desktop CLI can.
"""

from __future__ import annotations

import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from . import w2k2_download
from .cli import (
    _collect_samples,
    _discover_ebl_files,
    _dominant_source_only,
    _filter_to_dominant_engine,
    _iter_frames_for_path,
    _merge_by_source,
    _select_primary_gps_source,
    build_arg_parser,
)
from .geocode import NoGeocoder
from .html_writer import write_html_logbook
from .log import log, set_log_file, set_log_sink
from .marine import NoMarine
from .sample_cache import SampleCache
from .trip_ids import assign_trip_ids
from .tripbuilder import build_trips
from .weather import NoWeather


def run_pipeline(
    ebl_paths: List[str],
    output_html_path: str,
    sample_cache_path: str,
    boat_name: str,
    mmsi: str,
    call_sign: str,
    fetch_failed: bool = False,
) -> dict:
    """Decodes the given .ebl files, builds trips, and writes an HTML logbook to
    output_html_path. Geocoding/weather/marine lookups are always skipped for now (no settings
    toggle for them yet, and they'd otherwise ride the phone's cellular data every run -- see
    docs/android-app-plan.md's "Cellular data cost" note).

    fetch_failed mirrors --download-failed on the desktop CLI: set it when this run is showing
    the last-known-good logbook because a download attempt failed, so the page can mark itself as
    possibly stale (see html_writer.py) -- not used yet by the manual test button, but wired
    through for the periodic background sync to use later.

    Returns {"ok": True, "trip_count": N, "html_path": ...} on success, or
    {"ok": False, "error": "..."} for the same failure conditions cli.py's _run() already checks
    for (missing file, no position data, no trips)."""
    args = build_arg_parser().parse_args([])

    logfiles = [Path(p) for p in ebl_paths]
    if not logfiles:
        return {"ok": False, "error": "No .ebl files given."}

    fixes_by_source: dict = {}
    sogs_by_source: dict = {}
    depth_by_source: dict = {}
    water_temp_by_source: dict = {}
    battery_by_source: dict = {}
    attitude_by_source: dict = {}
    all_engine = []
    all_trip_fuel = []
    all_rpm = []

    ebl_time_state: dict = {}
    sample_cache = SampleCache(Path(sample_cache_path))
    for path in logfiles:
        if not path.exists():
            return {"ok": False, "error": f"Log file not found: {path}"}

        cached = sample_cache.get(path)
        if cached is not None:
            samples, time_state_after = cached
            fixes, sogs, engine, trip_fuel, depth, water_temp, battery, rpm, attitude = samples
            ebl_time_state["current"] = time_state_after
        else:
            frames = _iter_frames_for_path(path, ebl_time_state)
            samples = _collect_samples(frames)
            fixes, sogs, engine, trip_fuel, depth, water_temp, battery, rpm, attitude = samples
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

    sample_cache.save()

    # When this run actually happened, not the latest timestamp found in the data -- same
    # reasoning as _run() (see cli.py).
    latest_data_at = datetime.now(timezone.utc).replace(tzinfo=None)

    all_fixes, all_sogs, _primary_gps_source = _select_primary_gps_source(fixes_by_source, sogs_by_source)
    all_depth = _dominant_source_only(depth_by_source)
    all_water_temp = _dominant_source_only(water_temp_by_source)
    all_battery = _dominant_source_only(battery_by_source)
    all_attitude = _dominant_source_only(attitude_by_source)

    if not all_fixes:
        return {"ok": False, "error": "No position data (PGN 129025) found."}

    if args.engine_count == 1:
        all_engine, all_trip_fuel, all_rpm = _filter_to_dominant_engine(all_engine, all_trip_fuel, all_rpm)

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
        geocoder=NoGeocoder(),
        speed_threshold_kn=args.speed_threshold_kn,
        min_stop_minutes=args.min_stop_minutes,
        max_gap_minutes=args.max_gap_minutes,
        min_trip_distance_nm=args.min_trip_distance_nm,
        lock_radius_m=args.lock_radius_m if args.lock_radius_m >= 0 else None,
        lock_max_duration_minutes=args.lock_max_duration_minutes,
    )

    if not trips:
        return {"ok": False, "error": "No trips found (maybe never stopped or underway long enough)."}

    trip_uids = assign_trip_ids(trips, utc_offset_hours=args.utc_offset)

    html_path = Path(output_html_path)
    write_html_logbook(
        trips,
        html_path,
        boat_name=boat_name,
        mmsi=mmsi,
        call_sign=call_sign,
        utc_offset_hours=args.utc_offset,
        trip_uids=trip_uids,
        battery_warning_voltage=args.battery_warning_voltage,
        latest_data_at=latest_data_at,
        fetch_failed=fetch_failed,
        log_interval_minutes=args.log_interval_minutes,
        remarks_api_url=args.remarks_api_url,
        weather=NoWeather(),
        marine=NoMarine(),
        geocoder=NoGeocoder(),
        latest_position=all_fixes[-1] if all_fixes else None,
    )

    return {"ok": True, "trip_count": len(trips), "html_path": str(html_path)}


def sync_from_w2k2(
    user: str,
    password: str,
    subnet_prefix: str,
    download_dir: str,
    output_html_path: str,
    sample_cache_path: str,
    boat_name: str,
    mmsi: str,
    call_sign: str,
    progress_callback=None,
) -> dict:
    """Full sync for Android in one Chaquopy call: discover the W2K-2 on the given subnet,
    download any new/changed .ebl files, then run the decode/build/write pipeline. Reuses
    w2k2_download.py's existing, already-tested download orchestration (build_download_plan(),
    resuming a partial download) instead of re-implementing any of it in Kotlin -- Kotlin only
    needs to supply the hotspot's subnet prefix (from its own NetworkInterface detection) and the
    settings the user entered.

    subnet_prefix is required (unlike discover_w2k2()'s own default) -- self-detection would find
    the phone's cellular subnet, not the hotspot's, since both are active at once on Android.

    progress_callback, if given, is any object with report(current, total, file_name),
    isCancelled(), and onLogLine(line) methods -- typically a Kotlin object passed in through
    Chaquopy (Chaquopy lets Python call methods on an injected Java/Kotlin object like a normal
    Python object).
    report() is called once after each file that actually gets downloaded (not for ones already
    complete locally), with a 1-based current count out of the total this run will fetch -- lets
    the UI show real "N/M" progress instead of a single "bezig..." message for however long a
    first sync (the whole historical archive, easily gigabytes) takes.
    isCancelled() is checked before listing each folder's files and before starting each file's
    own download -- lets Kotlin stop a sync cleanly when the app closes (see SyncController.kt)
    instead of it continuing in the background. Not checked mid-transfer, so closing the app while
    one file is still downloading won't take effect until that transfer finishes.
    onLogLine() receives every line log() produces during this call verbatim (see log.py's
    set_log_sink()) -- the exact same "[info] Found W2K-2 at ...", "[info] <folder>: N file(s), M
    MB total", "[ok]"/"[skip]" per-file messages the desktop CLI prints, so the Android app can
    show the same messages instead of a separately-maintained set of Android-only text (asked for
    explicitly).

    Returns a dict: {"ok": False, "error": ...} if discovery/login/download failed, or was
    cancelled (with "cancelled": True), before any pipeline run was possible; otherwise
    run_pipeline()'s own result dict with an added "downloaded_count" key (how many of the W2K-2's
    files were actually fetched this run, as opposed to already being complete locally)."""
    # Persisted next to the logbook (filesDir on Android) so a run's full log survives after the
    # app closes and can be pulled off the device afterward (`adb pull`), the same way
    # nmea2log.log already works on desktop (see cli.py) -- console/logcat output alone is easy
    # to lose (found in practice: several live logcat captures during this same debugging session
    # died on a USB reconnect mid-run).
    set_log_file(Path(output_html_path).parent / "nmea2log.log")
    if progress_callback is not None:
        set_log_sink(progress_callback.onLogLine)
    try:
        return _sync_from_w2k2(
            user, password, subnet_prefix, download_dir, output_html_path, sample_cache_path,
            boat_name, mmsi, call_sign, progress_callback,
        )
    finally:
        set_log_sink(None)


def _sync_from_w2k2(
    user: str,
    password: str,
    subnet_prefix: str,
    download_dir: str,
    output_html_path: str,
    sample_cache_path: str,
    boat_name: str,
    mmsi: str,
    call_sign: str,
    progress_callback,
) -> dict:
    host = w2k2_download.discover_w2k2(subnet_prefix=subnet_prefix)
    if host is None:
        return {
            "ok": False,
            "error": f"No W2K-2 found on {subnet_prefix}0/24 -- is it joined to this hotspot?",
        }
    log(f"[info] Found W2K-2 at {host}")  # same message main() prints on desktop (see w2k2_download.py)

    config = w2k2_download.W2K2Config(download_dir=Path(download_dir), token=None, user=user, password=password)
    should_cancel = progress_callback.isCancelled if progress_callback is not None else None

    try:
        session = w2k2_download.make_session(host, config)
        plan, to_download = w2k2_download.build_download_plan(
            session, config.download_dir, should_cancel=should_cancel
        )
        to_download_total = len(to_download)

        downloaded_count = 0
        for folder_name, info in plan:
            target = config.download_dir / folder_name / info["file_name"]
            will_download = w2k2_download._will_download(target, info)
            w2k2_download.download_file(
                session, config.download_dir, folder_name, info, should_cancel=should_cancel,
            )
            if will_download:
                downloaded_count += 1
                if progress_callback is not None:
                    progress_callback.report(downloaded_count, to_download_total, info["file_name"])

        # Every locally-present .ebl file, not just the ones build_download_plan() actually
        # checked against the device this run -- a folder it skipped entirely (already complete
        # locally, see build_download_plan()) still needs its files fed into the pipeline below,
        # same as _run()'s own --ebl-dir discovery does on desktop (see cli.py).
        local_paths = [str(p) for p in _discover_ebl_files(config.download_dir)]
    except w2k2_download.DownloadCancelled:
        return {"ok": False, "error": "Sync cancelled.", "cancelled": True}
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"ok": False, "error": "401: token expired or invalid W2K-2 credentials."}
        return {"ok": False, "error": f"HTTP {exc.code}: {exc}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "error": f"Network error talking to the W2K-2: {exc}"}
    except SystemExit as exc:
        return {"ok": False, "error": str(exc)}

    result = run_pipeline(
        ebl_paths=local_paths,
        output_html_path=output_html_path,
        sample_cache_path=sample_cache_path,
        boat_name=boat_name,
        mmsi=mmsi,
        call_sign=call_sign,
    )
    result["downloaded_count"] = downloaded_count
    return result
