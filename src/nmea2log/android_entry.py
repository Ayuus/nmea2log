"""Android entry point: the same decode -> build trips -> write HTML pipeline as cli.py's
_run(), minus argparse and minus upload. Called from Kotlin via Chaquopy with a list of already-
downloaded .ebl files and settings read from EncryptedSharedPreferences, instead of reading
nmea2log.ini or parsing command-line flags.

run_pipeline() returns a plain dict instead of an exit code / raising SystemExit -- a background
sync (or the manual "Sync now" test button) can't rely on stderr text or a process return code
the way the desktop CLI can.
"""

from __future__ import annotations

import gc
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import w2k2_download
from .cli import (
    _DECODE_PROGRESS_INTERVAL_S,
    _MAX_RESUME_WIDEN_ATTEMPTS,
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
from .geocode import Geocoder
from .html_writer import write_html_logbook
from .log import log, set_log_file, set_log_sink
from .marine import MarineFetcher
from .sample_cache import SampleCache
from .trip_cache import TripCache, choose_resume_index, config_signature, find_resume_index
from .trip_ids import assign_trip_ids
from .tripbuilder import TRIP_LOGIC_VERSION, build_trips_chunked, resolve_trip_places
from .weather import WeatherFetcher


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
    should_cancel: Optional[Callable[[], bool]] = None,
    min_stop_minutes: Optional[float] = None,
) -> dict:
    """Decodes the given .ebl files, builds trips, and writes an HTML logbook to
    output_html_path -- geocoding, weather, and marine (wave/current) lookups are all enabled by
    default here, same as the desktop CLI's own default (no --no-geocode/--no-weather/--no-marine
    equivalent on Android yet -- asked for explicitly: app and desktop should behave as identically
    as possible, not a deliberately reduced feature set). Each rides a small, per-day-per-~1km-cell
    cache file next to the logbook itself (.geocode_cache.json/.weather_cache.json/
    .marine_cache.json, same file names cli.py itself defaults to), so a full season's worth of
    lookups only really costs cellular data once -- a later run over the same waters mostly hits
    the cache instead of the network. See docs/android-app-plan.md's "Cellular data cost" note if
    that ever needs its own settings toggle instead.

    min_stop_minutes overrides build_arg_parser()'s own default (see --min-stop-minutes on the
    desktop CLI) when given -- SettingsStore's own user-editable setting on Android, since there's
    no nmea2log.ini here for it to come from otherwise. None (the default) keeps the built-in
    default, same as not passing --min-stop-minutes at all.

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
    if min_stop_minutes is not None:
        args.min_stop_minutes = min_stop_minutes

    logfiles = [Path(p) for p in ebl_paths]
    if not logfiles:
        return {"ok": False, "error": "No .ebl files given."}

    # Same trip cache as cli.py's _run() (see trip_cache.py) -- a settled trip never changes, so
    # a normal day-to-day sync only needs to decode and rebuild the recent window since the last
    # known trip's own departure instead of the whole season every time. Kept next to the logbook
    # itself, same as the geocode/weather/marine caches below. This is the single biggest lever
    # on both this run's decode time and its peak memory (see the *_by_source cleanup further
    # down) -- found in practice: a real full-season sync peaked at ~3.7 GB RSS decoding and
    # rebuilding data that, for all but the most recent trip, never actually changes run to run.
    trip_signature = config_signature(
        speed_threshold_kn=args.speed_threshold_kn,
        min_stop_minutes=args.min_stop_minutes,
        max_gap_minutes=args.max_gap_minutes,
        min_trip_distance_nm=args.min_trip_distance_nm,
        min_leg_distance_nm=args.min_leg_distance_nm,
        lock_radius_m=args.lock_radius_m,
        lock_max_duration_minutes=args.lock_max_duration_minutes,
        language=args.language,
        engine_count=args.engine_count,
        trip_logic_version=TRIP_LOGIC_VERSION,
    )
    trip_cache_store = TripCache(Path(output_html_path).parent / ".trip_cache.pkl")
    settled_trips: list = []
    resume_index = 0
    resume_ebl_time_state_seed: object = None
    cached_trip_data = trip_cache_store.load(trip_signature)
    if cached_trip_data is not None:
        cached_settled_trips, resume_from_file, resume_ebl_time_state = cached_trip_data
        found_index = find_resume_index(logfiles, resume_from_file)
        if found_index is not None:
            settled_trips = cached_settled_trips
            resume_index = found_index
            resume_ebl_time_state_seed = resume_ebl_time_state
            log(
                f"[cache] Reusing {len(settled_trips)} already-settled trip(s); skipping "
                f"decode of {resume_index}/{len(logfiles)} file(s)."
            )

    sample_cache = SampleCache(Path(sample_cache_path))
    # One shared instance (not a fresh Geocoder() per call below) -- write_html_logbook()'s own
    # "latest position" lookup below then reuses build_trips()'s in-memory cache instead of
    # possibly re-requesting a position it (or a nearby one, same rounded cache key) already
    # looked up moments ago. Cached to a file next to the logbook itself, same as
    # sync_from_w2k2()'s nmea2log.log placement, so the cache survives between runs instead of
    # re-requesting every already-known place's name on every single sync.
    geocoder = Geocoder(cache_file=Path(output_html_path).parent / ".geocode_cache.json", language=args.language)

    # Retried with resume_index widened by one file at a time -- see the check right after
    # build_trips() below -- if a resumed window turns out to have started mid-transit rather
    # than genuinely at the start of a stay. See cli.py's own _run() for the full reasoning; this
    # mirrors it.
    widen_attempts = 0
    while True:
        # fixes_by_source/sogs_by_source accumulate as FixArray/SogArray (see fix_array.py), not
        # plain lists -- same reasoning as cli.py's _run(): position/speed are this app's
        # highest-cardinality sample types, and holding a season's worth of them as Python
        # objects rather than array.array columns is what actually got this app OOM-killed by
        # the phone's OS.
        fixes_by_source: Dict[int, FixArray] = {}
        sogs_by_source: Dict[int, SogArray] = {}
        depth_by_source: Dict[int, DepthArray] = {}
        water_temp_by_source: Dict[int, WaterTempArray] = {}
        battery_by_source: Dict[int, BatteryArray] = {}
        attitude_by_source: Dict[int, AttitudeArray] = {}
        all_engine = []
        all_trip_fuel = []
        all_rpm = []

        # The cache's own recorded seed only applies to the *original* resume point -- see
        # cli.py's own _run() for why a widened attempt starts one file earlier with no seed at
        # all instead.
        ebl_time_state: dict = {}
        if widen_attempts == 0 and resume_ebl_time_state_seed is not None:
            ebl_time_state["current"] = resume_ebl_time_state_seed

        last_progress_log = time.monotonic()
        file_first_time_by_index: Dict[int, Optional[datetime]] = {}
        ebl_time_state_before_file: dict = {}
        for idx, path in enumerate(logfiles, start=1):
            if should_cancel is not None and should_cancel():
                return {"ok": False, "error": "Sync cancelled.", "cancelled": True}
            file_index = idx - 1
            if file_index < resume_index:
                # Already fully represented by settled_trips -- never even touched, not even to
                # check the sample cache, which is exactly the decode-time and decompression cost
                # this trip cache exists to avoid paying every single run (see cli.py's own decode
                # loop, which this mirrors).
                continue
            if not path.exists():
                return {"ok": False, "error": f"Log file not found: {path}"}

            ebl_time_state_before_file[file_index] = ebl_time_state.get("current")

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

            file_first_time: Optional[datetime] = None
            for source_fixes in fixes.values():
                if source_fixes:
                    candidate_time = min(f.time for f in source_fixes)
                    file_first_time = candidate_time if file_first_time is None else min(file_first_time, candidate_time)
            file_first_time_by_index[file_index] = file_first_time

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

        all_fixes, all_sogs, _primary_gps_source = _select_primary_gps_source(fixes_by_source, sogs_by_source)
        all_depth = _dominant_source_only(depth_by_source)
        all_water_temp = _dominant_source_only(water_temp_by_source)
        all_battery = _dominant_source_only(battery_by_source)
        all_attitude = _dominant_source_only(attitude_by_source)

        # The four *_by_source dicts above are dead weight from here on: each all_* variable
        # already holds its own direct reference to the one source array it needs (see
        # _select_primary_gps_source/_dominant_source_only in cli.py -- "by_source[dominant]",
        # not a copy), so the dicts themselves now only hold every *non*-dominant source's full
        # array for nothing. On a boat with more than one device sending the same PGN (e.g. two
        # GPS antennas), that's real, otherwise-unreachable memory -- found in practice: a real
        # OOM kill (lmkd reaped this process at ~2.6 GB rss) landed inside build_trips() itself
        # just below, the single most memory-hungry phase of the whole run, so freeing this now
        # (not desktop, which has never once hit this ceiling) buys real headroom right where it
        # matters most. sample_cache stays alive across a widened retry (see the loop above), so
        # it's freed once, after the loop, not here.
        del fixes_by_source, sogs_by_source, depth_by_source, water_temp_by_source, battery_by_source
        del attitude_by_source
        gc.collect()

        if not all_fixes and not settled_trips:
            return {"ok": False, "error": "No position data (PGN 129025) found."}

        if args.engine_count == 1:
            all_engine, all_trip_fuel, all_rpm = _filter_to_dominant_engine(all_engine, all_trip_fuel, all_rpm)

        if all_fixes:
            # Found in practice: build_trips() (and write_html_logbook() below) can run for a real
            # stretch of time on a full multi-year archive (millions of merged GPS fixes) with zero
            # log output in between -- decode's own progress logging (see
            # _DECODE_PROGRESS_INTERVAL_S) stops the moment the last file is read, leaving nothing on
            # screen (or in nmea2log.log) to tell "still working" apart from "hung" or "already
            # crashed silently" for however long this phase takes on a phone's much slower CPU.
            log(f"[info] Reizen opbouwen uit {len(all_fixes)} GPS-posities...")
            fresh_trips = build_trips_chunked(
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
            # resume_index above) happened to contain no position data at all -- nothing left to
            # (re)build; settled_trips alone, below, is still a perfectly good result.
            fresh_trips = []

        # See cli.py's own _run() for the full reasoning: a resumed window whose own first trip
        # has no known stay before it started mid-transit, not genuinely at the very first thing
        # in the whole archive -- widen by one file and retry, popping settled_trips' own last
        # entry too if it's about to be reconstructed fresh as part of the wider window.
        if (
            resume_index > 0
            and fresh_trips
            and fresh_trips[0].depart_place == "Unknown (start outside log file)"
            and widen_attempts < _MAX_RESUME_WIDEN_ATTEMPTS
        ):
            widen_attempts += 1
            resume_index -= 1
            if settled_trips and widen_attempts == 1:
                # Popped exactly once, on the *first* widen only -- see cli.py's own _run() for
                # why every subsequent widen this same run is still about resolving that same one
                # trip's boundary, not a second, different trip to also un-freeze.
                settled_trips = settled_trips[:-1]
            log(
                f"[cache] Resumed window started mid-transit (no known stay before its first "
                f"trip) -- widening by one file ({logfiles[resume_index].name}) and retrying "
                f"(attempt {widen_attempts}/{_MAX_RESUME_WIDEN_ATTEMPTS})."
            )
            continue
        break

    del sample_cache
    gc.collect()

    trips = settled_trips + fresh_trips

    if not trips:
        return {"ok": False, "error": "No trips found (maybe never stopped or underway long enough)."}

    if trip_cache_store is not None and fresh_trips:
        # Every trip except the newest one is now eligible to freeze -- see trip_cache.py's
        # module docstring for why the newest trip specifically is never cached, even here.
        new_settled_trips = settled_trips + fresh_trips[:-1]
        resume_reference_time = (
            new_settled_trips[-1].arrive_time if new_settled_trips else fresh_trips[-1].depart_time
        )
        new_resume_index = choose_resume_index(
            sorted(file_first_time_by_index.items()), resume_reference_time, resume_index
        )
        new_resume_file = str(logfiles[new_resume_index].resolve())
        new_resume_state = ebl_time_state_before_file.get(new_resume_index)
        trip_cache_store.save(new_settled_trips, new_resume_file, new_resume_state, trip_signature)

    # Every run, over every trip -- settled trips (just loaded straight from
    # trip_cache_store.load() above, never touched by build_trips() at all this run) included,
    # not just the fresh ones -- see resolve_trip_places()'s own doc comment for why this can't
    # just happen once inside build_trips() and get cached alongside everything else about a
    # settled trip.
    trips = resolve_trip_places(trips, geocoder)

    # trips (a small, already-summarized list of TripLeg) is everything write_html_logbook()
    # below needs -- the season's worth of raw per-sample arrays that built it are pure dead
    # weight from here on, except the one still-needed latest_position value, captured first.
    # Same reasoning as the *_by_source cleanup above: real headroom for a phase that now also
    # runs real (not instant) geocoding/weather/marine network lookups per trip, extending how
    # long this data would otherwise sit in memory unused.
    latest_position = all_fixes[-1] if all_fixes else None
    del all_fixes, all_sogs, all_depth, all_water_temp, all_battery, all_attitude
    del all_engine, all_trip_fuel, all_rpm
    gc.collect()

    log(f"[info] {len(trips)} trip(s) found, writing logbook...")
    trip_uids = assign_trip_ids(trips, utc_offset_hours=args.utc_offset)

    # When the logbook is actually about to be written, not right after decode -- same reasoning
    # as _run() (see cli.py): geocoding/build_trips() can run for real extra minutes after decode
    # finishes, so a decode-time stamp could sit visibly behind when the page was actually
    # produced.
    latest_data_at = datetime.now(timezone.utc).replace(tzinfo=None)

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
        log_interval_minutes=args.log_interval_minutes,
        remarks_api_url=args.remarks_api_url,
        weather=WeatherFetcher(cache_file=html_path.parent / ".weather_cache.json"),
        marine=MarineFetcher(cache_file=html_path.parent / ".marine_cache.json"),
        geocoder=geocoder,
        latest_position=latest_position,
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
    progress_callback=None,
    min_stop_minutes: Optional[float] = None,
) -> dict:
    """Thin wrapper around run_pipeline() for the "show whatever's already on the phone" path
    (no W2K-2 discovery/download, see MainActivity.runOfflineBuild()) -- sets up the same log
    file and live-progress sink sync_from_w2k2() already does below. Found in practice: without
    this, a real (not cache-hit) decode here left the on-screen status stuck on a single static
    "Logboek opbouwen..." message for however long it took, instead of the same "...decoded X/Y
    logfile(s) so far" progress a normal sync already shows -- run_pipeline() was always logging
    those lines, there was just nothing on this call path listening for them.

    min_stop_minutes: see run_pipeline()'s own doc comment."""
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
            should_cancel=should_cancel,
            min_stop_minutes=min_stop_minutes,
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
    min_stop_minutes: Optional[float] = None,
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
    files were actually fetched this run, as opposed to already being complete locally).

    min_stop_minutes: see run_pipeline()'s own doc comment."""
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
            boat_name, mmsi, call_sign, progress_callback, min_stop_minutes,
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
    min_stop_minutes: Optional[float] = None,
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
            min_stop_minutes=min_stop_minutes,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Unexpected error while building the logbook: {exc}"}
    result["downloaded_count"] = downloaded_count
    return result


def discover_w2k2_only(subnet_prefix: str, progress_callback) -> None:
    """Chaquopy entry point for a plain "is the W2K-2 reachable right now" check -- the same
    network scan sync_from_w2k2() does as its own first step, but standing alone, with no
    download/decode/build attempted afterward. Used by the Android app to decide whether to
    enable its sync button, before committing to a real sync (asked for explicitly: a cheap,
    local-only "does this device's own hotspot look on" check isn't the same as the W2K-2 itself
    actually being reachable on it).

    Reports the result via progress_callback.onDiscoverResult(found: bool), not this call's own
    return value -- same reasoning as _report_result() above: reading a returned PyObject's
    fields after a Chaquopy call has already returned has been unreliable in this app, while
    calling into Kotlin *during* the call has not."""
    found = w2k2_download.discover_w2k2(subnet_prefix) is not None
    progress_callback.onDiscoverResult(found)
