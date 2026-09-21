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
from typing import Callable, List, Optional

from . import w2k2_download
from .cli import build_arg_parser
from .geocode import Geocoder
from .html_writer import write_html_logbook
from .log import log, set_log_file, set_log_sink
from .marine import MarineFetcher
from .pipeline import PipelineCancelled, PipelineError, discover_ebl_files, build_season_trips
from .trip_ids import assign_trip_ids
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

    Returns {"ok": True, "trip_count": N, "html_path": ..., "boat_state": {...} or None} on success
    (``boat_state`` = tripbuilder.BoatState.to_dict(): underway/stationary since, engine off since,
    last position, all as of the end of the data just processed),
    {"ok": False, "error": "Sync cancelled.", "cancelled": True} if should_cancel() said so
    partway through, or {"ok": False, "error": "..."} for the same failure conditions cli.py's
    _run() already checks for (missing file, no position data, no trips)."""
    args = build_arg_parser().parse_args([])
    if min_stop_minutes is not None:
        args.min_stop_minutes = min_stop_minutes

    # Sorted here, unconditionally -- unlike cli.py's own logfiles (discovered via
    # discover_ebl_files(), which already sorts), ebl_paths here can come straight from Kotlin's
    # own file listing (MainActivity's local file scan for the offline-build path; for the W2K-2
    # sync path it's discover_ebl_files() again, already sorted -- sorting twice is a no-op).
    # Found in practice, on a real device: Kotlin's own listing is not
    # guaranteed to be chronological (File.walkTopDown() makes no ordering promise), which doesn't
    # just affect decode order cosmetically -- every season-wide sample array accumulated below
    # (fixes/sogs/attitude/...) is built by appending each file's own records in this same
    # sequence, so an out-of-order logfiles list means out-of-order data, forcing every one of
    # those arrays to pay for their own defensive re-sort later (see
    # tripbuilder.py's _reject_gps_outliers_array/SogArray.drop_time_regressions/
    # AttitudeArray.drop_time_regressions) -- confirmed, via fine-grained
    # checkpoint logging on a real device with a real ~2.7M-position archive, to be exactly where a
    # full-archive rebuild was getting OOM-killed. Sorting the (~2000, not ~2 million) file paths
    # themselves, once, up front, is what actually keeps every one of those arrays already sorted
    # by construction, making that defensive re-sort a genuine no-op instead of a safety net that
    # always ends up paying its own real cost.
    logfiles = sorted(Path(p) for p in ebl_paths)
    if not logfiles:
        return {"ok": False, "error": "No .ebl files given."}

    # One shared instance (not a fresh Geocoder() per call below) -- write_html_logbook()'s own
    # "latest position" lookup below then reuses build_trips()'s in-memory cache instead of
    # possibly re-requesting a position it (or a nearby one, same rounded cache key) already
    # looked up moments ago. Cached to a file next to the logbook itself, same as
    # sync_from_w2k2()'s nmea2log.log placement, so the cache survives between runs instead of
    # re-requesting every already-known place's name on every single sync.
    output_dir = Path(output_html_path).parent
    geocoder = Geocoder(cache_file=output_dir / ".geocode_cache.json", language=args.language)

    # Same decode -> trips pipeline as the desktop CLI (see pipeline.py), with the trip cache kept
    # next to the logbook itself, same as the geocode/weather/marine caches. That trip cache is the
    # single biggest lever on both this run's decode time and its peak memory -- found in
    # practice: a real full-season sync peaked at ~3.7 GB RSS decoding and rebuilding data that,
    # for all but the most recent trip, never actually changes run to run.
    try:
        season = build_season_trips(
            logfiles,
            args,
            sample_cache_path=Path(sample_cache_path),
            trip_cache_path=output_dir / ".trip_cache.pkl",
            geocoder=geocoder,
            should_cancel=should_cancel,
        )
    except PipelineCancelled:
        return {"ok": False, "error": "Sync cancelled.", "cancelled": True}
    except PipelineError as exc:
        return {"ok": False, "error": str(exc)}
    trips = season.trips
    latest_position = season.latest_position

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
    log(f"[ok] Logbook written: {html_path} ({len(trips)} trip(s))")

    return {
        "ok": True,
        "trip_count": len(trips),
        "html_path": str(html_path),
        # Where the boat stands at the end of the data just processed (see tripbuilder.BoatState),
        # for the "on the boat" mode's harbour detection; None when there was no new position data.
        "boat_state": season.boat_state.to_dict() if season.boat_state is not None else None,
    }


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
        local_paths = [str(p) for p in discover_ebl_files(config.download_dir)]
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
