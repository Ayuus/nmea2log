# nmea2log

### Sailing logbook generator for the Actisense W2K-2

Pure Python application that turns NMEA2000 log files from an **Actisense W2K-2** into a
sailing logbook: departure/arrival port, fuel consumption (from engine data, not a tank sensor),
and engine hours. By default writes an **HTML logbook** (`.html`) — one self-contained file with
the boat name, totals (distance/fuel/engine hours/average consumption), trips grouped by
year/week, and a clickable, zoomable map per trip. Pass `--csv` and/or `--gpx` to also write a
**CSV** (`.csv`) and/or a **GPX** with the sailed route per trip (`.gpx`, opens in navigation
software like OpenCPN/Navionics) — same name as `-o`, different extension.

**[View an example logbook](https://htmlpreview.github.io/?https://github.com/Ayuus/nmea2log/blob/main/examples/demo-logbook.html)** — a fictional boat and trips ([examples/demo-logbook.html](examples/demo-logbook.html)), showing what the generated HTML output looks like.

> **Looking for testers**: so far this has only been run against one boat's NMEA2000 network — a
> **motorboat** (one Actisense W2K-2, one particular mix of engine/GPS/depth/battery instruments).
> Other boats report data differently enough — different instrument brands, different PGNs
> available, engines that report fuel rate differently, and so on — that edge cases are likely
> still hiding. Sailboat support in particular is on the wishlist (wind instruments, a boat that's
> often underway with the engine off) but untested so far. If you have a W2K-2 and try this on
> your own boat, feedback (what worked, what looked wrong, a `.ebl` file that fails to parse) is
> very welcome via [GitHub issues](https://github.com/Ayuus/nmea2log/issues).

> **Attribution**: the SD-card `.ebl` binary log format has never been officially published by
> Actisense. `ebl_reader.py` is a clean-room Python reimplementation based on reading the
> open-source Go library [aldas/go-nmea-client](https://github.com/aldas/go-nmea-client)
> (Apache-2.0 license), specifically
> [`actisense/eblreader.go`](https://github.com/aldas/go-nmea-client/blob/main/actisense/eblreader.go)
> — no code was copied from it, only the understanding of the framing/byte-stuffing/CAN-ID
> layout it documents. See "Assumptions & limitations" below for how this was validated against
> real data.

No runtime dependency beyond the Python standard library — only `pytest` as a dev dependency for
the tests.

## How it works

1. **Reading** (`ebl_reader.py`): `.ebl` files, the binary format of the W2K-2's **SD card
   logging feature** (BST-95 CAN-raw). The app has to reassemble NMEA2000 Fast Packet frames
   itself here. This format is reverse-engineered (see "Assumptions & limitations"), but has
   since been validated against real SD card logs from a W2K-2 with a Yanmar 4LV195Z engine: a
   complete cold engine start (fuel rate, oil pressure buildup, warming up, engine-hour meter,
   even the "Preheat Indicator" warning during glow-plug preheating) came out physically
   plausible and internally consistent.
2. **Decoding** (`pgn_decode.py`): picks ten PGNs out of the stream:
   - **127489** (*Engine Parameters, Dynamic*) → fuel rate, engine-hour meter, and health
     indicators (oil pressure/temperature, coolant temperature, alternator voltage, engine load)
     plus the two "Discrete Status" warning fields. This is engine data, so explicitly not the
     tank-level sensor.
   - **127488** (*Engine Parameters, Rapid Update*) → engine speed (RPM), sent much more
     frequently than PGN 127489's other fields, so decoded as its own sample stream.
   - **127497** (*Trip Parameters, Engine*) → optional: the trip-meter fuel reading the
     engine/ECU keeps itself (in liters), if the device sends this PGN.
   - **128267** (*Water Depth*) → water depth under the transducer.
   - **129025** (*Position, Rapid Update*) → GPS position.
   - **129026** (*COG & SOG, Rapid Update*) → speed over ground (SOG, GPS-derived). This is
     explicitly not "speed through water" (that would be PGN 128259, a paddlewheel/log sensor —
     not used by this app, and not present on the bus of the boat tested so far either).
   - **126992** (*System Time*) → only used for `.ebl` files, to give frames an absolute
     date/time (see below).
   - **130312** (*Temperature*) → sea/outside water temperature, filtered to that specific
     "source" (the same PGN can also carry cabin, exhaust gas, etc. temperature, which is
     ignored).
   - **127508** (*Battery Status*) → a dedicated battery monitor's own voltage reading, distinct
     from PGN 127489's alternator voltage (that's the engine's charging output, only present
     while the engine is running; this keeps reporting at anchor with the engine off too).
   - **127257** (*Attitude*) → pitch and roll, from whatever motion sensor (autopilot, gyro
     compass) is already on the network.
3. **Recognizing trips** (`tripbuilder.py`): periods where the boat is stationary for long
   enough (default ≥ 10 minutes, adjustable via `--min-stop-minutes`) count as a port visit; the
   periods in between are the trips. A large gap in the data itself (default also 10 minutes,
   separately adjustable via `--max-gap-minutes`) always cuts a trip short, even if the
   stationary period right before the gap was too short to count as a port visit on its own —
   otherwise a trip would bridge a gap with a far-too-long reported duration (found in practice:
   3:21 instead of the real ~0:45, while the engine-hour meter — which doesn't depend on GPS
   classification — had it right). Trips shorter than 0.1 nm (adjustable via
   `--min-trip-distance-nm`) are filtered out: that's almost always GPS/speed noise right at such
   a segment boundary, not a real trip.

   A stop is treated as a lock, opening bridge, or similarly brief operational pause — folded
   back into the trip instead of showing up as a separate port visit — if the engine was off no
   longer than `--lock-max-duration-minutes` (default 120) and the boat stayed within
   `--lock-radius-m` (default 10) of its own position for the *entire* time the engine was off,
   not just however much of that time GPS/speed noise happened to leave classified as
   "stationary". This only ever applies to a stop that comes after an actual trip (never the very
   first thing in the log) and only if the engine is confirmed running again afterwards — an
   engine that never restarts is either a genuine arrival or simply where the log ends, never a
   lock. There's no reliable way to know an exact position is a lock (reverse geocoding and the
   Overpass API both proved too unreliable for this), so this is a deliberately simple heuristic:
   it also folds away any other brief, tightly-confined pause where the engine happens to cycle
   off and on again the same day (e.g. a quick stop at a quay) — pass a negative
   `--lock-radius-m` to disable it entirely and always show every stop ≥ `--min-stop-minutes` as
   a port visit. Per trip, the app calculates:
   - **Fuel consumption**, two ways: **calculated** by integrating the fuel-rate reading
     (PGN 127489) over time, and — if available — the difference between the start and end
     reading of the **engine's own trip meter** (PGN 127497). Note: that trip meter is a counter
     the engine manages itself and may have been reset by the user on the display, so it doesn't
     necessarily match our own departure/arrival split exactly.
   - **Engine hours**: the difference between the engine-hour meter at departure and arrival.
     Engine hours, typical RPM, engine health, and warnings are shown per engine instance
     ("engine 0: ...", "engine 1: ...") as soon as more than one instance shows up in the data;
     with exactly one engine that label is dropped automatically. If a second instance shows up
     anyway even though you only have one engine (a duplicate/ghost source), set
     `--engine-count 1` to ignore it.
   - **Typical RPM**: the most commonly occurring engine speed during the trip (rounded to the
     nearest 50 RPM before counting) -- a more representative "cruising RPM" than an average
     (skewed by idle/neutral periods and maneuvering) or a maximum (skewed by brief revs). In the
     HTML logbook, hovering over it shows the speed range recorded during *sustained* runs
     (at least 2 minutes) at that RPM, since the trip's overall average speed is diluted by
     slower maneuvering and can otherwise read as if that RPM only makes that (lower) speed; a
     brief pass through that RPM while accelerating/decelerating doesn't count as holding it.
     The range can still be wide for a long cruise at a constant RPM (found in practice: over
     two hours at a steady 2250 RPM legitimately ranged from 8 to 17.6 kn with the tide) -- that's
     real, not a bug.
   - **Engine health**: average oil pressure/temperature, coolant temperature, alternator
     voltage, and maximum engine load during the trip, plus a separate **warnings** column with
     all active status flags (e.g. "Low Oil Pressure") that occurred at any point during the
     trip.
   - **Battery voltage**: average and minimum voltage during the trip, per battery instance. If
     the minimum drops below `--battery-warning-voltage` (default 12.2 V) at any point, that
     also shows up in the same **warnings** column (e.g. "low battery 11.8 V").
   - **Speed**: average and maximum speed over ground.
   - **Water temperature**: average, minimum, and maximum sea temperature during the trip. Shown
     in the HTML logbook as a plain number, with the color-coded thermometer badge (and the
     min-max range, if notable) as a hover tooltip rather than always inline.
   - **Roll/pitch variation**: standard deviation of roll and pitch (PGN 127257) during the trip
     — a rougher sea or more wave action shows up as more variation in how the boat's attitude
     moves around, even if the average heel/trim stays level. Also reports the peak-to-peak
     range (max - min) alongside it: a trip that's mostly calm with one rough patch still
     averages out to a small standard deviation, so the range captures the single worst swing
     instead (found in practice: a trip with a 2.5° standard deviation still had a 23° roll
     range). Shown in the HTML logbook as the standard deviation, with the peak range as a hover
     tooltip. Neither is an established metric like significant wave height (which needs an
     actual wave sensor this app doesn't have) — just relative indicators from whatever motion
     sensor is already on the network.
   - **Minimum water depth**, including the position where it was measured.
4. **Port names** (`geocode.py`): the GPS position of each port visit is turned into a place
   name via OpenStreetMap/Nominatim (reverse geocoding), with local caching so the same position
   is never looked up twice. If the nearest match is more than 250 m away (common when
   anchoring/mooring away from any mapped harbour, e.g. in a sparsely-mapped bay), the name is
   prefixed with "op het water, bij" (on the water, near) or "aan de kant, bij" (alongside, near)
   instead of silently implying the boat was right there -- which of the two depends on whether
   the matched feature's OSM type is something boats actually tie up to (marina, harbour, quay,
   ...) or not. This is a coarse heuristic (there's no coastline data to check against), not a
   real "is the boat touching the shore" measurement.
5. **Weather** (`weather.py`): each row of the periodic "Log" table also gets wind speed/direction,
   precipitation and cloud cover for that position and hour, looked up from Open-Meteo's free
   historical weather archive (a regional weather model, not an on-board sensor -- so treat it as
   indicative, not exact) and cached locally so the same position/hour is never looked up twice.
6. **Marine data** (`marine.py`): the same "Log" table rows also get wave height/period/direction
   and ocean current speed/direction, looked up from Open-Meteo's separate marine weather API
   (same caching/indicative-not-exact caveats as the weather data above).
7. **Writing the logbook** (`logbook_writer.py`, only with `--csv`): CSV with English column names
   but Dutch Excel convention for the values (`;` as the delimiter, `,` as the decimal separator)
   — opens correctly right away in Dutch-locale Excel.
8. **Writing the route** (`gpx_writer.py`, only with `--gpx`): a GPX file (same file name, `.gpx`
   extension) with one track per trip. Click a track in a map program and you see a name and
   description with duration, distance, fuel, and engine hours for that trip.
9. **HTML logbook** (`html_writer.py`): one self-contained `.html` file (same file name, `.html`
   extension) — no separate map file or workbook needed anymore. At the top, the boat name
   (`--boat-name`, or the `boat_name` setting in the config file) and totals: trip count, total
   distance, fuel, average consumption, top speed, **engine hour meter** (the absolute reading
   of the engine's own hour counter as of the most recently logged trip — handy for maintenance
   intervals, so it also counts hours the engine ran before you started logging) and "hours
   logged" (summed over only the trips in this logbook). The same totals are repeated per
   calendar year, right under that year's heading, so you can also see a season at a glance
   rather than only the all-time numbers. Below that, the trips grouped by year and ISO week.
   Each trip with a track has a "Map" button that opens a zoomable Leaflet/OpenStreetMap map with
   the route line inline, embedded in the same page. Map tiles and the Leaflet library come from
   a CDN, so **viewing** requires internet (generating doesn't). Trips with a logged water
   temperature also get a colored badge (blue → red by temperature). Each trip with a track also
   has a "Log" button — a traditional periodic logbook table (time, position, course over ground,
   speed, and, unless `--no-weather`/`--no-marine` is passed, wind/precipitation/cloud cover and
   wave/current data), sampled every `--log-interval-minutes` (default 30) plus always the trip's
   own start and end. Unlike the map, this needs no JavaScript or internet to display (native HTML
   `<details>`), so it also works when the file is opened from an email attachment.

## Installation

Requires Python 3.9 or newer. One-time setup, from the project directory:

```bash
pip install -e ".[test]"
```

This installs three commands (`nmea2log`, `nmea2log-download`, and their shared library) in
editable mode, so pulling a code update doesn't require reinstalling. No other dependency gets
installed — `pytest` (for the tests) is the only thing pulled in beyond the Python standard
library. On Windows, run this once from a terminal (PowerShell or Command Prompt) with `py` on
the `PATH`; after that, the `.bat` files below don't need a terminal at all.

## Configuration

Both `nmea2log-download` (downloading from the W2K-2) and `nmea2log` itself (processing defaults)
read their settings from one shared config file, `nmea2log.ini` **in the project directory**.
It's in `.gitignore` and so never gets committed — create it yourself (there's no template
checked in, since it holds your device password):

```ini
[w2k2]
; Username + password (the download command logs in with these automatically).
user = admin
password = your-password-here
; Folder the downloaded .ebl files land in (structure EBL000000/, EBL000001/, ... created
; underneath). Also in .gitignore.
download_dir = Actisense

[nmea2log]
; Boat name, shown at the top of the HTML logbook.
boat_name = Zeevalk
; Same folder as download_dir above, so `nmea2log` with no arguments finds the files on its own
; after downloading -- no more selecting or dragging files by hand.
ebl_dir = Actisense
```

The W2K-2's IP address is never configured -- `nmea2log-download` finds it automatically every
run by scanning this machine's own local subnet for a host whose web interface identifies itself
as Actisense/W2K-2 (see `discover_w2k2` in `w2k2_download.py`). This only works while this
machine is on the same wifi network as the W2K-2 (either its own access point, or a shared boat/
home wifi router both are joined to as clients).

Every setting in `[nmea2log]` can also be passed as a command-line flag instead (see "Useful
options" below); an explicit flag always overrides what's in the config file. `[w2k2]` only has
the three keys shown above (see `w2k2_download.py` for the optional `token` alternative to
user/password).

Note: if the project directory is synced (e.g. OneDrive, as in the original setup this was built
for), the password in `nmea2log.ini` syncs along to the cloud/your other PC too — a deliberate
tradeoff for convenience. If you don't want that, keep the file outside the synced folder instead
and pass `--config path/to/nmea2log.ini` (for `nmea2log`) or `nmea2log-download --config
path/to/nmea2log.ini`.

## Usage

### Windows quick start

Once configured (see above), day-to-day use is one double-click, no terminal needed:

- **`nmea2log.bat`** — downloads any new `.ebl` files from the W2K-2, then processes everything
  under `ebl_dir` into `logbook.html`. If the boat isn't reachable (no wifi), it prints a message
  and just processes whatever's already local instead of getting stuck — nothing to babysit. Add
  `--csv`/`--gpx` (e.g. by editing the shortcut/command) if you also want those files.
  Drag a single log file onto it instead to skip both the download and the `ebl_dir` search, and
  process just that one file.

The rest of this section explains what this does underneath, and the full command-line options,
for other platforms or more control.

### Processing log files

**From the SD card**: download the `.ebl` files, either manually via the W2K-2's web interface
("Download Logs"), or automatically with the included `nmea2log-download` command (reads
`nmea2log.ini`, see "Configuration" above):

```bash
nmea2log-download
```

The command only downloads what's still missing or incomplete (compared by file size), so
running it again after a later sail only fetches the new files.

Then process the downloaded files as usual:

```bash
nmea2log Actisense/EBL000000/*.ebl Actisense/EBL000001/*.ebl -o logbook.csv
```

Or, simpler: run `nmea2log` with no arguments at all. It then searches the `ebl_dir` folder from
`nmea2log.ini` recursively for `.ebl` files — set once, so after downloading you never have to
select or drag files by hand again.

This EBL path is reverse-engineered (see "Assumptions & limitations") and has since been
validated against real SD card logs — when in doubt, always check that the outcome feels
plausible for your own boat/engine.

This writes `logbook.html` (the self-contained HTML logbook with totals, year/week grouping, and
clickable maps). Add `--csv` and/or `--gpx` to also get `logbook.csv` and/or `logbook.gpx` (the
route per trip).

Processing multiple files at once (e.g. one per day):

```bash
nmea2log Actisense/EBL000000/000000_014.ebl Actisense/EBL000000/000000_015.ebl -o logbook.csv
```

### Per-trip remarks, login-gated, via WordPress

**Remarks only work when publishing over WordPress REST (`--upload-rest`), not over SFTP** -- the
feature is backed by the WordPress plugin below, which has nothing to talk to on a site published
over plain SFTP.

The preferred way to publish: pass `--remarks-api-url` to add a "Remarks" button+popup
(save/cancel) to each trip, backed by a small WordPress plugin
(`wordpress-plugin/nmea2log-remarks.php`) instead of a database or server of this tool's own.
This also login-gates the whole logbook, not just remarks: the uploaded file goes to a `private/`
directory outside the public web root, and `wordpress-plugin/logboek-index.php` (deployed as
e.g. `www/logboek/index.php`) checks the visitor is both logged in and specifically allowed to
view the logbook, redirecting to the WordPress login page (not logged in) or showing a plain
access-denied message (logged in as some unrelated account, e.g. a webshop customer) otherwise.

Multiple boats can share one WordPress site: which logbook a person sees (or a Logbook Writer
edits/uploads) is resolved from *who's logged in*, via a "boat" field on their own user profile --
not from the URL, so the gatekeeper page below is deployed exactly once no matter how many boats
use the site.

**Installing the plugin** (one-time setup on the WordPress site):

1. Upload `wordpress-plugin/nmea2log-remarks.php` to `wp-content/plugins/nmea2log-remarks/` and
   activate it in wp-admin → Plugins. It registers two purpose-built roles -- deliberately not
   reusing any built-in WordPress role, since those can already be in use for unrelated things on
   an existing site (webshop customers, existing contributors, ...): "Logbook Writer" (can view,
   save remarks, and upload/publish) and "Logbook Reader" (can only view). A site Administrator
   can always do both, without needing either role.
2. Optional: pick a site-wide default `<slug>` in wp-admin → Instellingen → nmea2log, used for
   anyone with no boat of their own (mainly an Administrator). Defaults to `logboek` if left
   blank. (Or, if you'd rather not store it in the database at all, add
   `define('NMEA2LOG_SLUG', 'your-boat');` to `wp-config.php` instead -- that takes priority and
   disables the field there.)
3. Create each boat owner's account, in wp-admin → Users, with the "Logbook Writer" role, and set
   their own `<slug>` (a per-boat identifier -- letters/digits/hyphens) in the "nmea2log" section
   on their Edit User profile screen. A Writer can then invite/remove their *own* boat's readers
   themselves, from "Mijn lezers" in the wp-admin sidebar -- no further Administrator involvement
   needed per reader.
4. Upload `wordpress-plugin/logboek-index.php` as `index.php` and `wordpress-plugin/logboek-
   views.php` as `views.php` into one shared location under `www/` (e.g. `www/logboek/`) -- both
   resolve the right boat per visitor at request time, so there's nothing inside either file
   itself to adjust for your own layout, and nothing to repeat per boat.
5. On each Writer's own profile page (wp-admin → Users → Profile → Application Passwords),
   generate a new Application Password -- a long, auto-generated credential scoped to this one
   integration, not the account's real login password. Copy it now; WordPress only shows it once.

**Using it** (publishing from `nmea2log`): pass `--upload-rest` plus the REST endpoint and that
Application Password. The REST endpoint writes to `private/<that account's own slug>/logbook.html`,
*outside* the public web root (e.g. `private/` on TransIP webhosting, which already isn't served
over HTTP), automatically -- nothing to configure client-side for the path itself.

```bash
nmea2log --upload-rest --upload-rest-url https://your-site.example/wp-json/nmea2log/v1/logbook \
  --upload-rest-user my-writer-account --upload-rest-app-password "xxxx xxxx xxxx xxxx xxxx xxxx" \
  --remarks-api-url /wp-json/nmea2log/v1/remarks -o logbook.csv
```

Or set all four once in the config file's `[upload]` section instead of typing them every time
(see "Configuration" above -- `hostname`/`username`/`password` there map to
`--upload-rest-url`/`--upload-rest-user`/`--upload-rest-app-password`; filling in all three there
also turns `--upload-rest` on by default, no separate flag needed).

`--remarks-api-url` takes a relative URL, resolved against whatever site the page is opened from,
so no separate host needs configuring as long as the logbook is uploaded to the same site as the
plugin. Reading and saving remarks both ride on the visitor's existing WordPress login session
(cookie + a nonce the login-gate script patches into the page at serve time) -- there's no
separate username/password entered in the popup itself.

### Uploading over SFTP instead

An older, still-supported alternative to the WordPress/REST route above, for a plain website with
no WordPress on it: pass `--upload` to copy the generated HTML logbook to a website over SFTP
right after writing it, so it's viewable from anywhere without running a server of your own (no
port-forwarding or dynamic DNS needed for a home connection). Requires an SSH key pair for
authentication -- a login password can't be scripted through the `sftp` client without an
interactive prompt, which defeats the point of running this unattended. Uses the system's own
`sftp` client (OpenSSH, already installed on Windows 10/11 and Debian), not an extra dependency.
Note: this only uploads the file itself -- no login gate, no per-trip remarks; anyone with the URL
can view it.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
```

Add the resulting `~/.ssh/id_ed25519.pub` to your hosting provider's SSH/SFTP access settings
(for TransIP webhosting: control panel → Webhosting → your domain → Website → SFTP/SSH → "+ Key
toevoegen"), then either pass the connection details on the command line or set them once in the
config file's `[upload]` section (see `nmea2log.ini`):

```bash
nmea2log --upload --upload-host ssh.example.transip.nl --upload-user my-user \
  --upload-remote-path logboek/logbook.html --upload-key-file ~/.ssh/id_ed25519 -o logbook.csv
```

### Useful options

| Option | Meaning |
|---|---|
| `--csv` | Also write the CSV logbook (default: only the HTML logbook is written) |
| `--gpx` | Also write the GPX route file (default: only the HTML logbook is written) |
| `--speed-threshold-kn` | Speed (kn) below which the boat counts as 'stationary' (default 0.5) |
| `--min-stop-minutes` | Minimum stationary duration to count as a port visit (default 10) |
| `--max-gap-minutes` | From how many minutes without data a trip gets cut short (default: same as `--min-stop-minutes`) |
| `--min-trip-distance-nm` | Trips shorter than this are filtered out as noise instead of shown (default 0.1 nm) |
| `--lock-radius-m` | Max drift (meters) during an engine-off stop for it to count as a lock/bridge instead of a port visit (default 10; negative disables it) |
| `--lock-max-duration-minutes` | Max engine-off duration for a confined stop to still count as a lock/bridge (default 120, i.e. 2 hours) |
| `--no-geocode` | No internet needed; shows coordinates instead of port names |
| `--cache-file` | Path to the cache file for port names (default `.geocode_cache.json`) |
| `--no-weather` | No internet needed; the Log table's wind/precipitation/cloud cover columns stay empty |
| `--weather-cache-file` | Path to the cache file for historical weather (default `.weather_cache.json`) |
| `--no-marine` | No internet needed; the Log table's wave/current columns stay empty |
| `--marine-cache-file` | Path to the cache file for historical wave/current data (default `.marine_cache.json`) |
| `--utc-offset HOURS` | Fixed timezone offset (e.g. `2` for CEST) for the displayed times. Default: automatically estimated per trip from the departure position |
| `--boat-name NAME` | Boat name at the top of the HTML logbook (default: none, or the `boat_name` setting from the config file) |
| `--mmsi MMSI` | MMSI at the top of the HTML logbook (default: none, or the `mmsi` setting from the config file) |
| `--call-sign SIGN` | Call sign at the top of the HTML logbook (default: none, or the `call_sign` setting from the config file) |
| `--log-interval-minutes` | Interval between periodic course/speed/position entries in each trip's "Log" table (default 30) |
| `--upload-rest` | Upload the HTML logbook to a WordPress REST endpoint after writing it (see "Per-trip remarks, login-gated, via WordPress" above) |
| `--upload-rest-url` / `--upload-rest-user` / `--upload-rest-app-password` | WordPress REST connection details (only with `--upload-rest`) |
| `--upload` | Upload the HTML logbook over SFTP after writing it instead (see "Uploading over SFTP instead" above) |
| `--upload-host` / `--upload-user` / `--upload-remote-path` / `--upload-key-file` / `--upload-port` | SFTP connection details (only with `--upload`; default port 22) |
| `--remarks-api-url` | URL of a WordPress REST endpoint storing per-trip remarks (see "Per-trip remarks" above). Default: disabled |
| `--engine-count N` | Number of physical engines. With `1`, any extra engine instance in the data is ignored as noise (same idea as the GPS source-dominance filtering) |
| `--ebl-dir DIR` | Folder to search recursively for `.ebl` files when no logfiles are given. Default: not set, or the `ebl_dir` setting from the config file |
| `--battery-warning-voltage V` | Flags a trip's battery voltage as low in the 'Warnings' column if it drops below this at any point (default 12.2 V; a common threshold for a 12V lead-acid battery -- adjust for a 24V system or a different chemistry) |

All NMEA2000 times are UTC; in the CSV, the HTML logbook, and the GPX track names this is
converted to local time. Without `--utc-offset`, the offset is estimated per trip from the
departure position: within Western Europe it recognizes the actual CET/CEST vs. WET/WEST civil
timezones (France, for instance, is geographically in the same longitude band as the UK but
observes Central European Time, a full hour off from what longitude alone would suggest),
including EU summer time (last Sunday of March through last Sunday of October, 01:00 UTC — a
fixed, recurring rule computable without a timezone database, so no extra dependency needed).
Outside that region it falls back to a plain solar-longitude estimate. This is still just an
estimate (can be off by up to ~1 hour near a timezone border, and the summer-time assumption
doesn't hold outside Europe, e.g. the Caribbean or the US) — use `--utc-offset` to force a fixed
value yourself if that matters to you. `<trkpt><time>` in the GPX always stays strictly UTC, per
the GPX convention. The "duration" column is offset-independent (it's a span, not a point in
time).

Any of the options above can also be set as a default in `nmea2log.ini`'s `[nmea2log]` section
instead of typing them every time (see "Configuration" near the top) — a command-line flag always
overrides the config file.

## Tests

```bash
pytest
```

## Assumptions & limitations

- **`nmea2log-download` API**: like the EBL file format itself, the W2K-2's web API
  (`/api/data_logs`, `/api/download`, login/token) has never been officially published by
  Actisense — observed via browser DevTools on the firmware web app, and may change with
  firmware updates. In particular, the field name the login token comes back under isn't 100%
  confirmed (`w2k2_download.py` tries a number of common names, see `_TOKEN_KEYS`); if login
  succeeds but no token is found, the command shows the raw response so the right name can be
  added.
- **W2K-2 discovery**: `discover_w2k2` scans this machine's own /24 subnet (up to 254 addresses,
  ~1 second with concurrent connections) for a host answering on port 80 whose page identifies
  itself as Actisense/W2K-2. This assumes a /24 network (true for basically every home/boat
  router) and that this machine is on the same subnet as the W2K-2 -- it won't find a device
  behind a different router or VLAN. It can't distinguish between multiple W2K-2 units on the
  same network (picks whichever answers first); not a concern with a single device per boat.
- **EBL format (SD card log)**: this format has never been officially published by Actisense.
  `ebl_reader.py` is based on reverse-engineering by the open-source Go library
  [aldas/go-nmea-client](https://github.com/aldas/go-nmea-client) (Apache-2.0 license;
  specifically
  [`actisense/eblreader.go`](https://github.com/aldas/go-nmea-client/blob/main/actisense/eblreader.go) —
  framing, byte-stuffing, CAN-ID decoding; see the attribution note at the top of this file) —
  hand-verified against the test vectors in it, and since validated against ~800 MB of real SD
  card logs from a W2K-2 (Yanmar 4LV195Z sterndrive): a complete cold engine start came out
  physically plausible and internally consistent (fuel rate, oil pressure buildup, voltage sag
  during cranking, warming up, an engine-hour meter that tracked exactly, and even a "Preheat
  Indicator" warning exactly during glow-plug preheating). Known limitations/assumptions:
  - The format's own 2-byte per-record time counter is ignored (its meaning isn't reliably
    documented anywhere — even the reference implementation guesses at it). Instead, absolute
    time is derived from PGN 126992 (System Time) found elsewhere in the stream. **Consequence**:
    if your NMEA2000 network has no source that sends PGN 126992 (usually a GPS/chartplotter),
    an `.ebl` file yields nothing — frames before the first 126992 message are skipped, and with
    no 126992 at all, no frames come out.
  - Fast Packet reassembly (needed for PGN 127489 and 127497, both >8 bytes) is implemented per
    the standard NMEA2000 convention and has since also been validated against real fast-packet
    data (see above).
  - If you get unexpected results, share a small `.ebl` fragment and it can be checked against
    real data together.
- **Port recognition** is based on how long the boat stays stationary plus reverse geocoding, not
  a list of known marinas. Nominatim doesn't always return the exact marina name (sometimes the
  name of the nearest built-up area instead). For more precise names, the next step would be
  adding your own port list (name + coordinates + radius) that gets checked first. Nominatim's
  usage policy allows at most 1 request/second; that's respected, but for heavy/commercial use a
  dedicated Nominatim instance or a paid service is better. If you get wrong names, check the raw
  cache in `.geocode_cache.json`.
- **Weather data** comes from Open-Meteo's historical archive, which snaps each lookup to the
  nearest weather-model grid cell — confirmed in practice to be several km off from the requested
  position — so treat it as "roughly what conditions were like nearby", not a precise reading at
  the boat's exact spot. If you get wrong-looking values, check the raw cache in
  `.weather_cache.json`.
- **Marine data** (wave/current) comes from Open-Meteo's separate marine weather API, with the
  same grid-snapping caveat as the weather data above -- indicative, not exact. Ocean current
  velocity has no server-side knots conversion (confirmed against the real API: unlike wind's
  `wind_speed_unit=kn`, a `current_velocity_unit=kn` parameter is silently ignored and km/h still
  comes back), so it's converted to knots in `marine.py` instead. If you get wrong-looking values,
  check the raw cache in `.marine_cache.json`.
- **Multiple engines**: the code supports multiple `instance` numbers (fuel is summed, engine
  hours shown per engine separately), but hasn't been tested with a real twin-engine
  installation.
- **Only one run at a time**: `nmea2log` refuses to start if another run against the same output
  directory is already in progress (a `.nmea2log.lock` file next to the output, holding that
  run's process id) -- two runs racing on the shared sample cache and HTML output can otherwise
  silently produce a wrong result instead of a clear error (found in practice: launching
  `nmea2log.bat` again because a slow run looked stuck, while the first was still working,
  produced a live logbook with only 1 of 15 real trips). If you get the "already in progress"
  error and you're sure nothing is actually still running (e.g. a previous run was killed), delete
  the `.nmea2log.lock` file mentioned in the error and try again.
- **Multiple sources for the same PGN**: some boats have multiple devices sending position,
  speed over ground, or depth (e.g. two GPS antennas). This has been measured and confirmed with
  real data: on a boat with two GPS receivers, they gave a few meters of position difference at
  the same moment (median 3.2 m, max 7.0 m over ~6000 comparisons) and a fraction of a knot of
  speed difference (median 0.2 kn, max 1.9 kn over three sources). Small on its own, but without
  filtering those independent readings get interleaved purely by time, causing thousands of
  small false "jumps" — in practice this initially inflated a trip's distance 10x (153.7 instead
  of 13.5 nm).

  **How the app handles this** (`_select_primary_gps_source` in `cli.py`): the source with the
  most position messages (PGN 129025) counts as the **primary GPS**, and the speed (PGN 129026)
  from **that same physical source** is used — deliberately not choosing the "best" source
  independently per PGN, since then position and speed could come from two different devices and
  create a hard-to-diagnose inconsistency between the track and the stationary/underway
  classification. Only if the chosen position source itself doesn't send speed does the code fall
  back to the speed source with the most messages (so then a different device after all). Depth
  is chosen independently (not a GPS-related PGN, so no reason to tie it to the same source). The
  CLI reports on stderr which source was chosen as primary whenever there's more than one.
  **Caveat**: "most messages" is a proxy, not a quality assessment — GPS accuracy (HDOP,
  satellite count, fix type) isn't taken into account.
- **Fuel consumption** is calculated from engine fuel-rate data (PGN 127489, integrated over
  time) or the engine's own trip counter (PGN 127497) — nothing is shown when a boat has no
  engine-reporting PGNs at all, which matters for sailboats that are often underway with the
  engine off. A fuel tank level sensor (PGN 127505, not currently parsed anywhere in this
  codebase) could fill that gap, but isn't the more accurate choice even on boats that have one:
  differencing tank level before/after a single trip is noisy (fuel sloshing while
  underway/heeling, coarse resistive-sender resolution, non-linear tank shapes) and only really
  settles out over many refuels, not one trip. Future work: parse PGN 127505 as a *fallback* for
  when engine fuel-rate data is absent, clearly labeled as a rougher estimate rather than shown
  the same way as engine-derived figures.
- **Water depth**: the app uses the raw "Depth" value from PGN 128267 (depth under the
  transducer), without adding the transducer offset — usually that's already the value
  instruments show by default, but check this against your own depth-sounder settings.
- **Engine warnings**: the bit meanings (e.g. "Low Oil Pressure") come from the generic NMEA2000
  standard list (canboat's ENGINE_STATUS_1/2). Some manufacturers use deviating or additional
  proprietary status bits for this — check this against your own engine documentation if a
  warning shows up unexpectedly or is missing.

## Working with Claude Code on this project

- Work in small, verifiable steps: run `pytest` after every change before moving on — the test
  layout above (`tests/`) is specifically there to make that fast.
- When adding new features, provide concrete example data (a snippet of real or realistic log
  lines), especially for anything involving PGN decoding — that saves guesswork.
- Once you have a real log file from the W2K-2: share a small fragment (a few hundred lines is
  enough) so the parsing format and PGN assumptions can be verified against real data.
