"""Android entry point: the same decode -> build trips -> write HTML pipeline as cli.py's
_run(), minus argparse and minus upload. Called from Kotlin via Chaquopy with a list of already-
downloaded .ebl files and settings read from EncryptedSharedPreferences, instead of reading
nmea2log.ini or parsing command-line flags.

run_pipeline() returns a plain dict instead of an exit code / raising SystemExit -- a background
sync (or the manual "Sync now" test button) can't rely on stderr text or a process return code
the way the desktop CLI can.
"""

from __future__ import annotations

import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import w2k2_download
from .cli import (
    _DECODE_PROGRESS_INTERVAL_S,
    _collect_samples,
    _discover_ebl_files,
    _dominant_source_only,
    _filter_to_dominant_engine,
    _iter_frames_for_path,
    _merge_array_by_source,
    _select_primary_gps_source,
    build_arg_parser,
)
from .fix_array import AttitudeArray, BatteryArray, DepthArray, FixArray, SogArray, WaterTempArray
from .geocode import NoGeocoder
from .html_writer import write_html_logbook
from .log import log, set_log_file, set_log_sink
from .marine import NoMarine
from .sample_cache import SampleCache
from .trip_ids import assign_trip_ids
from .tripbuilder import build_trips
from .weather import NoWeather


def _report_result(progress_callback, result: dict) -> None:
    """Hands the final outcome to progress_callback.onResult(...) as individual primitive
    arguments, in addition to still returning the dict as before -- found in practice, real and
    reproducible (not tied to memory pressure: seen both after a full multi-hour sync and after
    cancelling one within seconds, during plain file listing): Kotlin reading result.get("ok")
    etc. *after* this call had already returned sometimes came back with the wrong values (ok as
    false, error as null, even when this dict clearly has ok=True/a real error string), while
    every *other* Chaquopy interaction on this same object -- report(), onLogLine(),
    isCancelled(), all called *during* this call instead of after it returns -- has never once
    shown that problem all session. Calling into Kotlin here, one last time, while still inside
    this same call, sidesteps whatever goes wrong with reading a returned PyObject's fields
    after the fact, instead of trying to explain it.

    -1 stands in for a Python None on the two int fields (trip_count/downloaded_count) -- passing
    an actual None across as a boxed Integer works fine through Chaquopy, but Kotlin's onResult()
    can accept a plain non-nullable Int this way instead of needing an Int? overload."""
    if progress_callback is None:
        return
    progress_callback.onResult(
        bool(result.get("ok", False)),
        result.get("error"),
        bool(result.get("cancelled", False)),
        result.get("trip_count") if result.get("trip_count") is not None else -1,
        result.get("html_path"),
        result.get("downloaded_count") if result.get("downloaded_count") is not None else -1,
    )


def run_pipeline(
    ebl_paths: List[str],
    output_html_path: str,
    sample_cache_path: str,
    boat_name: str,
    mmsi: str,
    call_sign: str,
    fetch_failed: bool = False,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Decodes the given .ebl files, builds trips, and writes an HTML logbook to
    output_html_path. Geocoding/weather/marine lookups are always skipped for now (no settings
    toggle for them yet, and they'd otherwise ride the phone's cellular data every run -- see
    docs/android-app-plan.md's "Cellular data cost" note).

    fetch_failed mirrors --download-failed on the desktop CLI: set it when this run is showing
    the last-known-good logbook because a download attempt failed, so the page can mark itself as
    possibly stale (see html_writer.py) -- not used yet by the manual test button, but wired
    through for the periodic background sync to use later.

    should_cancel, if given, is checked periodically during the decode loop -- found in practice:
    closing the app (see MainActivity.closeAppAndCancelSync()) sets SyncState.cancelled, but that
    had no effect at all once decoding had started, since only the earlier download loop (see
    _sync_from_w2k2()) ever checked it. A long decode (1000+ files, especially on a phone's
    slower CPU) could then run on for many more minutes after the user thought they'd stopped it.

    Returns {"ok": True, "trip_count": N, "html_path": ...} on success,
    {"ok": False, "error": "Sync cancelled.", "cancelled": True} if should_cancel() said so
    partway through, or {"ok": False, "error": "..."} for the same failure conditions cli.py's
    _run() already checks for (missing file, no position data, no trips)."""
    args = build_arg_parser().parse_args([])

    logfiles = [Path(p) for p in ebl_paths]
    if not logfiles:
        return {"ok": False, "error": "No .ebl files given."}

    # fixes_by_source/sogs_by_source accumulate as FixArray/SogArray (see fix_array.py), not
    # plain lists -- same reasoning as cli.py's _run(): position/speed are this app's highest-
    # cardinality sample types, and holding a season's worth of them as Python objects rather
    # than array.array columns is what actually got this app OOM-killed by the phone's OS.
    fixes_by_source: Dict[int, FixArray] = {}
    sogs_by_source: Dict[int, SogArray] = {}
    depth_by_source: Dict[int, DepthArray] = {}
    water_temp_by_source: Dict[int, WaterTempArray] = {}
    battery_by_source: Dict[int, BatteryArray] = {}
    attitude_by_source: Dict[int, AttitudeArray] = {}
    all_engine = []
    all_trip_fuel = []
    all_rpm = []

    ebl_time_state: dict = {}
    sample_cache = SampleCache(Path(sample_cache_path))
    last_progress_log = time.monotonic()
    for idx, path in enumerate(logfiles, start=1):
        if should_cancel is not None and should_cancel():
            return {"ok": False, "error": "Sync cancelled.", "cancelled": True}
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

        # A phone's CPU decodes far slower than a desktop's -- found in practice: a run whose
        # cache hadn't (yet) been saved from a prior, interrupted attempt spent several silent
        # minutes here with nothing on screen to tell a real decode from a hang (see
        # _DECODE_PROGRESS_INTERVAL_S in cli.py, shared so both platforms behave the same way).
        now = time.monotonic()
        if now - last_progress_log >= _DECODE_PROGRESS_INTERVAL_S and idx < len(logfiles):
            log(f"[info] ...decoded {idx}/{len(logfiles)} logfile(s) so far")
            last_progress_log = now

        _merge_array_by_source(fixes_by_source, fixes, FixArray)
        _merge_array_by_source(sogs_by_source, sogs, SogArray)
        all_engine += engine
        all_trip_fuel += trip_fuel
        _merge_array_by_source(depth_by_source, depth, DepthArray)
        _merge_array_by_source(water_temp_by_source, water_temp, WaterTempArray)
        _merge_array_by_source(battery_by_source, battery, BatteryArray)
        all_rpm += rpm
        _merge_array_by_source(attitude_by_source, attitude, AttitudeArray)

    # Unconditional, unlike the in-loop progress line above (which deliberately skips the very
    # last file) -- found in practice: since that in-loop line is also time-gated, the previous
    # progress line could be a couple of seconds stale even when decode genuinely finished
    # cleanly, leaving no explicit confirmation the last file was ever reached rather than the
    # run having silently died one file short.
    log(f"[info] ...decoded {len(logfiles)}/{len(logfiles)} logfile(s) so far")

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

    # Found in practice: build_trips() (and write_html_logbook() below) can run for a real
    # stretch of time on a full multi-year archive (millions of merged GPS fixes) with zero log
    # output in between -- decode's own progress logging (see _DECODE_PROGRESS_INTERVAL_S) stops
    # the moment the last file is read, leaving nothing on screen (or in nmea2log.log) to tell
    # "still working" apart from "hung" or "already crashed silently" for however long this phase
    # takes on a phone's much slower CPU.
    log(f"[info] Reizen opbouwen uit {len(all_fixes)} GPS-posities...")
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

    log(f"[info] {len(trips)} trip(s) found, writing logbook...")
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
    log(f"[ok] Logboek geschreven: {html_path} ({len(trips)} reis/reizen)")

    return {"ok": True, "trip_count": len(trips), "html_path": str(html_path)}


def build_from_local_files(
    ebl_paths: List[str],
    output_html_path: str,
    sample_cache_path: str,
    boat_name: str,
    mmsi: str,
    call_sign: str,
    fetch_failed: bool = False,
    progress_callback=None,
) -> dict:
    """Thin wrapper around run_pipeline() for the "show whatever's already on the phone" path
    (no W2K-2 discovery/download, see MainActivity.runOfflineBuild()) -- sets up the same log
    file and live-progress sink sync_from_w2k2() already does below. Found in practice: without
    this, a real (not cache-hit) decode here left the on-screen status stuck on a single static
    "Logboek opbouwen..." message for however long it took, instead of the same "...decoded X/Y
    logfile(s) so far" progress a normal sync already shows -- run_pipeline() was always logging
    those lines, there was just nothing on this call path listening for them."""
    set_log_file(Path(output_html_path).parent / "nmea2log.log")
    if progress_callback is not None:
        set_log_sink(progress_callback.onLogLine)
    should_cancel = progress_callback.isCancelled if progress_callback is not None else None
    try:
        result = run_pipeline(
            ebl_paths=ebl_paths,
            output_html_path=output_html_path,
            sample_cache_path=sample_cache_path,
            boat_name=boat_name,
            mmsi=mmsi,
            call_sign=call_sign,
            fetch_failed=fetch_failed,
            should_cancel=should_cancel,
        )
    finally:
        set_log_sink(None)
    _report_result(progress_callback, result)
    return result


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
    isCancelled(), onLogLine(line), and onDownloadComplete() methods -- typically a Kotlin object
    passed in through Chaquopy (Chaquopy lets Python call methods on an injected Java/Kotlin object
    like a normal Python object).
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
    onDownloadComplete() is called exactly once, right after the last file's download attempt and
    before run_pipeline() (decode/build/write) starts -- lets Kotlin drop its own network-activity
    indicator (the foreground sync notification) once there's no more network I/O left in this
    call, since decode/build is pure CPU (see MainActivity.runSync()).

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
        result = _sync_from_w2k2(
            user, password, subnet_prefix, download_dir, output_html_path, sample_cache_path,
            boat_name, mmsi, call_sign, progress_callback,
        )
    finally:
        set_log_sink(None)
    _report_result(progress_callback, result)
    return result


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
    should_cancel = progress_callback.isCancelled if progress_callback is not None else None

    try:
        # discover_w2k2() itself used to sit outside this try block -- found in practice: whatever
        # it raised (a real one seen on a memory-pressured device: the subnet scan's own thread
        # pool failing to start a worker thread) then had nothing at all to catch it, propagating
        # uncaught past Kotlin's own generic try/except the exact same way the exceptions below
        # already had a fix for once (see the broad except Exception at the bottom of this block,
        # and its own commit message) -- Kotlin's result.error ended up null, and showSyncResult()
        # had nothing better than a bare "onbekende fout" to show for it.
        host = w2k2_download.discover_w2k2(subnet_prefix=subnet_prefix)
        if host is None:
            return {
                "ok": False,
                "error": f"No W2K-2 found on {subnet_prefix}0/24 -- is it joined to this hotspot?",
            }
        log(f"[info] Found W2K-2 at {host}")  # same message main() prints on desktop (see w2k2_download.py)

        config = w2k2_download.W2K2Config(download_dir=Path(download_dir), token=None, user=user, password=password)
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

        # Downloading is done -- decode/build below is pure CPU, no network involved, so the
        # caller can drop its own network-activity indicator now (Android: the foreground sync
        # notification, kept up only for as long as it's actually covering network I/O, see
        # MainActivity.runSync() -- a "dataSync" foreground service has a real cumulative time
        # budget on Android 15+, no reason to keep spending it once there's no more network work
        # left in this call).
        if progress_callback is not None:
            progress_callback.onDownloadComplete()

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
    # Catch-all, deliberately last and deliberately broad: found in practice that a flaky
    # connection can raise something none of the specific handlers above catch (e.g. a garbled/
    # truncated response body failing json.loads() with a plain ValueError, not an OSError) --
    # without this, that propagated all the way past Kotlin's own generic try/except in
    # MainActivity.runSync() as a raw PyException and left result.error as Kotlin null, which
    # showSyncResult() then had nothing better to show than a bare "onbekende fout" (asked for
    # explicitly to fix: that text is never actually informative). Every failure path here must
    # end up with a real, specific message instead.
    except Exception as exc:
        return {"ok": False, "error": f"Unexpected error: {exc}"}

    # A second, separate try/except from the download one above (rather than one big block
    # covering both): found in practice that this call -- decode, build_trips(), write_html_logbook()
    # -- had no exception handling of its own at all. Anything raised here propagated uncaught
    # all the way past Kotlin's own generic try/except in MainActivity.runSync(), which (unlike a
    # normally-returned {"ok": False, "error": ...} dict) never reaches showSyncResult() at all --
    # so instead of even the "onbekende fout" fallback dialog, the failure only ever showed up as
    # plain status text ("Onverwachte fout tijdens synchroniseren: ...") with no way to try the
    # existing "toon logboek met bestaande data" recovery option. Every failure path here must
    # end up with a real, specific message returned normally instead.
    try:
        result = run_pipeline(
            ebl_paths=local_paths,
            output_html_path=output_html_path,
            sample_cache_path=sample_cache_path,
            boat_name=boat_name,
            mmsi=mmsi,
            call_sign=call_sign,
            should_cancel=should_cancel,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Unexpected error while building the logbook: {exc}"}
    result["downloaded_count"] = downloaded_count
    return result
