# Android app for nmea2log

## Context

`nmea2log` today is a manual, desktop-driven workflow: plug into the boat's wifi, run `nmea2log.bat`, wait, done. The owner wants this automated on a phone instead: the phone runs its own mobile hotspot, the W2K-2 (configured in wifi "Client Mode") joins that hotspot, and the app should notice when that's happened, pull new `.ebl` files, rebuild the HTML logbook, and publish it — all without the owner doing anything, and without draining the battery by polling constantly. Trigger, in the owner's own words: "alleen als hotspot aan en clients connected, alleen hotspot clients checken" (only when the hotspot is on and has connected clients).

Because the phone is *both* hosting the hotspot for the W2K-2 *and* normally still has its own cellular uplink (that's how tethering works), download (over the hotspot) and upload (over 4G/5G) don't need a "wait until back on real wifi" state machine — they can happen back-to-back in the same run.

The existing Python package (`src/nmea2000processor/`) has **zero third-party runtime dependencies** — confirmed by `pyproject.toml` (`dependencies = []`) and by grepping every import in all 19 source files (stdlib only: `urllib`, `socket`, `concurrent.futures`, `configparser`, `pickle`, `zlib`, etc.). That makes embedding it as-is via **Chaquopy** (runs a real CPython interpreter inside an Android app, callable from Kotlin) the right call, instead of a full rewrite. Two things don't port over unchanged: `upload.py` shells out to the system `sftp` binary (doesn't exist on Android), and `config.py`/`cli.py`'s INI-file + argparse plumbing (CWD-relative, meaningless in an app sandbox).

**Scope of this plan, per the owner's answers**: background sync only (no in-app logbook viewer — viewing stays on the existing ayuus.com page), credentials in `EncryptedSharedPreferences`/`EncryptedFile`, phone-hosts-hotspot topology.

## Biggest open risk — validate before building anything real

Reliably detecting "hotspot has a connected client" from a normal (non-system) Android app is genuinely uncertain:

- `WifiManager.registerSoftApCallback()` for the system's *own* tethering hotspot needs `NETWORK_SETTINGS`/`NETWORK_STACK` — privileged permissions a third-party app can't obtain. Very likely a hard wall, not just "unreliable."
- `/proc/net/arp` is increasingly locked down since Android 10.

**Recommended approach: don't solve "is there a client" as its own problem — fold it into discovery.** Detect "hotspot is on" via `java.net.NetworkInterface` enumeration (no special permission: when tethering is active, the AP interface — `ap0`/`wlan1`/`swlan0` depending on OEM — shows up `isUp()==true` with a private IPv4). That interface's address + prefix *is* the hotspot's subnet, which also fixes a real bug the current desktop `discover_w2k2()` would have on a phone: its subnet-detection trick (UDP-connect to 8.8.8.8) would find the **cellular** subnet, not the hotspot's, when both are active. So: pass the hotspot's subnet explicitly instead of relying on auto-detection. Then just run the same port-80 probe `discover_w2k2()` already does, scoped to that subnet — if nothing answers, there's either no hotspot or no W2K-2 client joined; either way, nothing to do. One mechanism, not two.

Layer `WIFI_AP_STATE_CHANGED` (dynamically registered, not manifest-declared) on top as a fast-path when the app is already alive, but the durable trigger is a `PeriodicWorkRequest` (WorkManager's 15-minute floor — a hard platform constraint). **Always ship a manual "Sync now" button** as the reliable baseline, not just a fallback.

**Phase 0 spikes (throwaway code, before any real app scaffolding):**
1. Confirm `registerSoftApCallback` throws `SecurityException` for a normal app on the actual target device (expect it to fail).
2. Confirm `NetworkInterface` enumeration reliably reports the AP interface + correct subnet with tethering on, on the actual target device — this is the load-bearing assumption of the whole design; must be verified for real, not assumed.
3. Bare Chaquopy spike: embed the unmodified `nmea2000processor` package, call `ebl_reader.iter_frames()` on a bundled sample `.ebl` from Kotlin. Validates the single biggest architectural bet.
4. Minimal sshj put+rename against the real TransIP server with the real Ed25519 key.

If spike 2 fails, fall back to WIFI_AP_STATE_CHANGED + a fixed periodic scan interval while the hotspot is on (simpler, slightly less battery-optimal, much less risky) — decide this only after the spike, not preemptively.

## SFTP replacement

**sshj (hierynomus/sshj)**, not jsch (unmaintained, no Ed25519 — and the project's own README setup uses `ssh-keygen -t ed25519`) and not Apache MINA SSHD (overkill for put+rename+ls). Register `org.bouncycastle:bcprov-jdk18on` as the `Security` provider at startup for consistent Ed25519 support across OEM crypto quirks.

A `SftpUploader` Kotlin object mirrors `upload.py`'s behavior for parity:
- `uploadAtomic()` — put to `$remotePath.tmp-upload`, then `rename`, matching the atomic-upload fix already in `upload.py` (`src/nmea2000processor/upload.py`, `upload_file()`).
- `listRemoteFilenames()` / `uploadFiles()` with the same chunking/retry shape as `upload_files()`/`list_remote_filenames()` (chunk size 25, 2 retries, 5s backoff) for `--backup-ebl` parity.
- Host key verification: trust-on-first-use, pin the fingerprint in `EncryptedSharedPreferences`, reject later mismatches (replicates `-o StrictHostKeyChecking=accept-new`).
- Private key imported once via Storage Access Framework, stored as an `EncryptedFile` — never on shared storage.
- Don't port the banner-stripping error-message parsing in `_sftp_error_message()` (`upload.py`) — that's specific to scraping the OpenSSH CLI's stderr text; sshj surfaces errors differently, re-validate against the real server once ported.

## Module split

**Chaquopy-embedded Python, unchanged** (point Chaquopy's Gradle plugin directly at `src/nmea2000processor` as its source, not a copy, so desktop CLI and app share one source of truth): `ebl_reader.py`, `pgn_decode.py`, `model.py`, `tripbuilder.py`, `trip_ids.py`, `geocode.py`, `weather.py`, `marine.py`, `gpx_writer.py`, `logbook_writer.py`, `html_writer.py`, `sample_cache.py`, `log.py`.

**Small changes to two existing files** (desktop CLI behavior must not change):
- `src/nmea2000processor/w2k2_download.py` — give `discover_w2k2()` an optional `subnet_prefix: Optional[str] = None` param; `None` keeps today's self-detection, an explicit value (always supplied on Android) skips `_local_subnet_prefix()`.
- Fix `sample_cache.py`'s `save()` to write via temp-file-then-rename instead of a direct `Path.write_bytes()` — Android can kill a background process mid-write far more aggressively than a desktop OS; the *load* path already tolerates corruption safely, the *write* path currently doesn't. Worth doing regardless of platform, small and safe.

**New Python** (`src/nmea2000processor/android_entry.py` or similar): `run_pipeline(ebl_dir, output_html_path, boat_name, mmsi, call_sign, w2k2_subnet_prefix, w2k2_credentials, ...) -> RunResult` — does what `cli.py`'s `_run()` does, minus argparse, minus the PID-based `.nmea2log.lock` (see below), minus the upload step (Kotlin does that via sshj after this returns), returning a structured result Kotlin can read instead of stderr text.

**Not ported at all**: `upload.py` (replaced by `SftpUploader`), `config.py`'s INI reading (replaced by `EncryptedSharedPreferences`), the PID-based lock in `cli.py` (`_acquire_lock`/`_release_lock`) — `WorkManager.enqueueUniqueWork(name, ExistingWorkPolicy.KEEP, ...)` gives the same "only one run at a time" guarantee at the platform level and fits the in-process job model better than a PID file does.

**New Kotlin**:
- `PipelineWorker : CoroutineWorker` — the WorkManager entry point: detect hotspot subnet → run Python pipeline → sshj upload → notify.
- `HotspotDetector` — `NetworkInterface` enumeration (Phase 0, spike 2).
- `PythonPipelineRunner` — thin Chaquopy bridge calling `android_entry.run_pipeline(...)`.
- `SftpUploader` — sshj wrapper, as above.
- Credential/settings storage: `EncryptedSharedPreferences` (W2K-2 user/password/token, SFTP host/user/path/port, pinned host-key fingerprint, boat name/MMSI/call sign) + `EncryptedFile` (SSH private key) — full replacement for `nmea2log.ini`. Settings UI to enter these once.
- Notifications: success + trip count, or specific failure (not found / login failed / upload failed) — `--download-failed`'s red-timestamp marking (`html_writer.py`) is a natural pattern to reuse when download fails but a locally-cached logbook can still be shown as "stale."

## Phased build

0. **Spikes** (throwaway code, real target device): the 4 listed above. Don't scaffold the real app until 2 and 3 are confirmed.
1. **Scaffold**: new Kotlin project, Chaquopy wired to `src/nmea2000processor`, settings UI + encrypted storage, WorkManager skeleton with a no-op worker (validates scheduling/battery behavior independent of the pipeline).
2. **Wire the pipeline, manual trigger first**: `android_entry.run_pipeline()`, `HotspotDetector`, `PythonPipelineRunner`, `SftpUploader`, a "Sync now" button. Test against the real W2K-2 and real SFTP server end-to-end before automating anything.
3. **Automate**: periodic WorkManager trigger + `WIFI_AP_STATE_CHANGED` fast path. Needs real multi-day on-the-water testing — can't be fully validated on a bench.
4. **Polish**: notifications, retry/error UX, `--backup-ebl` parity, process-death-mid-run handling (WorkManager's retry policy covers most of this already).

## Other things to keep in mind while building

- **Chaquopy licensing**: free for open-source/non-commercial; a closed-source commercial release needs a paid license. Confirm the intended distribution model before committing further.
- **Chaquopy footprint**: bundling CPython adds real APK size (tens of MB) and interpreter cold-start cost — irrelevant for a job running a few times a day, just don't be surprised by it.
- **Cellular data cost**: geocoding (Nominatim/Overpass) and weather/marine (Open-Meteo) lookups now always ride cellular, not home wifi. Worth a settings toggle mirroring `--no-geocode`/`--no-weather`/`--no-marine`.
- **`w2k2_download.make_session()`'s `input()`/`getpass.getpass()` fallback** must never be reachable from `run_pipeline()` — always pass credentials explicitly; add a defensive check rather than relying on remembering.
- **Scoped storage**: `.ebl` files and all caches/output live in `context.filesDir` (app-internal) — no runtime storage permission needed, and Chaquopy's Python takes plain absolute path strings fine. Don't lean on any `config.py` CWD-relative default.
- **OEM battery managers** (Samsung/MIUI/OxygenOS etc.) delay WorkManager periodic work well beyond stock AOSP Doze behavior — test on the actual device, expect to guide the user through a battery-optimization exemption.

## Verification

- Phase 0: each spike has its own pass/fail criterion (listed above) on a real device — no unit tests needed for throwaway spike code.
- Phase 2 (manual trigger): run the full pipeline against the real W2K-2 and real SFTP server via the "Sync now" button; compare the resulting HTML logbook's trip count/content against a desktop `nmea2log` run over the same `.ebl` files for parity.
- Phase 3 (automatic trigger): multi-day real sailing test — toggle hotspot on/off, join/leave the W2K-2, confirm sync happens without manual intervention and without a noticeable battery drain over a full day idle.
- Existing desktop test suite (`pytest`, run from the repo root) must stay green throughout — the shared-file changes (`w2k2_download.py`'s new optional param, `sample_cache.py`'s atomic write) are the only places Android work touches code the desktop CLI also depends on.
