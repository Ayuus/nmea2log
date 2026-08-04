# nmea2log

Pure Python application that turns NMEA2000 log files from an **Actisense W2K-2** into a
sailing logbook: departure/arrival port, fuel consumption (from engine data, not a tank sensor),
and engine hours. Writes three files (same name as `-o`, different extension): a **CSV**
(`.csv`), a **GPX** with the sailed route per trip (`.gpx`, opens in navigation software like
OpenCPN/Navionics), and an **HTML logbook** (`.html`) — one self-contained file with the boat
name, totals (distance/fuel/engine hours/average consumption), trips grouped by year/week, and a
clickable, zoomable map per trip.

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

1. **Reading** — two file formats, chosen automatically by extension:
   - `.ebl` (`ebl_reader.py`): the binary format of the W2K-2's **SD card logging feature**
     (BST-95 CAN-raw). The app has to reassemble NMEA2000 Fast Packet frames itself here. This
     format is reverse-engineered (see "Assumptions & limitations"), but has since been
     validated against real SD card logs from a W2K-2 with a Yanmar 4LV195Z engine: a complete
     cold engine start (fuel rate, oil pressure buildup, warming up, engine-hour meter, even the
     "Preheat Indicator" warning during glow-plug preheating) came out physically plausible and
     internally consistent.
   - everything else, e.g. `.raw`/`.n2k` (`ascii_reader.py`): an *N2K ASCII* log file, such as
     you can capture with `--live --tee`. Each line has already been reassembled by the Actisense
     hardware (fast-packet/multi-packet), so no reassembly is needed there.
2. **Decoding** (`pgn_decode.py`): picks eight PGNs out of the stream:
   - **127489** (*Engine Parameters, Dynamic*) → fuel rate, engine-hour meter, and health
     indicators (oil pressure/temperature, coolant temperature, alternator voltage, engine load)
     plus the two "Discrete Status" warning fields. This is engine data, so explicitly not the
     tank-level sensor.
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
3. **Recognizing trips** (`tripbuilder.py`): periods where the boat is stationary for long
   enough (default ≥ 10 minutes, adjustable via `--min-stop-minutes`) count as a port visit; the
   periods in between are the trips. A large gap in the data itself (default also 10 minutes,
   separately adjustable via `--max-gap-minutes`) always cuts a trip short, even if the
   stationary period right before the gap was too short to count as a port visit on its own —
   otherwise a trip would bridge a gap with a far-too-long reported duration (found in practice:
   3:21 instead of the real ~0:45, while the engine-hour meter — which doesn't depend on GPS
   classification — had it right). Trips shorter than 0.1 nm (adjustable via
   `--min-trip-distance-nm`) are filtered out: that's almost always GPS/speed noise right at such
   a segment boundary, not a real trip. Per trip, the app calculates:
   - **Fuel consumption**, two ways: **calculated** by integrating the fuel-rate reading
     (PGN 127489) over time, and — if available — the difference between the start and end
     reading of the **engine's own trip meter** (PGN 127497). Note: that trip meter is a counter
     the engine manages itself and may have been reset by the user on the display, so it doesn't
     necessarily match our own departure/arrival split exactly.
   - **Engine hours**: the difference between the engine-hour meter at departure and arrival.
     Engine hours, engine health, and warnings are shown per engine instance ("engine 0: ...",
     "engine 1: ...") as soon as more than one instance shows up in the data; with exactly one
     engine that label is dropped automatically. If a second instance shows up anyway even
     though you only have one engine (a duplicate/ghost source), set `--engine-count 1` to
     ignore it.
   - **Engine health**: average oil pressure/temperature, coolant temperature, alternator
     voltage, and maximum engine load during the trip, plus a separate **warnings** column with
     all active status flags (e.g. "Low Oil Pressure") that occurred at any point during the
     trip.
   - **Battery voltage**: average and minimum voltage during the trip, per battery instance. If
     the minimum drops below `--battery-warning-voltage` (default 12.2 V) at any point, that
     also shows up in the same **warnings** column (e.g. "low battery 11.8 V").
   - **Speed**: average and maximum speed over ground.
   - **Water temperature**: average, minimum, and maximum sea temperature during the trip.
   - **Minimum water depth**, including the position where it was measured.
4. **Port names** (`geocode.py`): the GPS position of each port visit is turned into a place
   name via OpenStreetMap/Nominatim (reverse geocoding), with local caching so the same position
   is never looked up twice.
5. **Writing the logbook** (`logbook_writer.py`): CSV with English column names but Dutch Excel
   convention for the values (`;` as the delimiter, `,` as the decimal separator) — opens
   correctly right away in Dutch-locale Excel.
6. **Writing the route** (`gpx_writer.py`): alongside the CSV, a GPX file is always written too
   (same file name, `.gpx` extension) with one track per trip. Click a track in a map program and
   you see a name and description with duration, distance, fuel, and engine hours for that trip.
7. **HTML logbook** (`html_writer.py`): one self-contained `.html` file (same file name, `.html`
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
   temperature also get a colored badge (blue → red by temperature).

## Installation

```bash
pip install -e ".[test]"
```

## Usage

The W2K-2 has three independent "data servers" (in the device's web interface, default ports
60001-60003). Set one to **protocol TCP** and **format N2K ASCII** — you can then use it in two
ways:

### Option A: stored log files

**From the SD card** (no live connection needed — recommended if you don't want to depend on a
connection while sailing): download the `.ebl` files, either manually via the W2K-2's web
interface ("Download Logs"), or automatically with the included `nmea2log-download` command:

```bash
nmea2log-download
```

This reads settings (IP address/hostname, username+password or a token, target folder) from a
config file, by default `nmea2log.ini` **in the current directory** (usually the project
directory), so you don't have to type them in every time. `nmea2log.ini` is in `.gitignore` and
so is never committed. Note: this project directory is in OneDrive though, so a password here
syncs along to the cloud/your other PC — a deliberate choice; if you don't want that, pass
`--config path/outside/onedrive/nmea2log.ini` instead. The command only downloads what's still
missing or incomplete (compared by file size), so running it again after a later sail only
fetches the new files. By default the files land in `Actisense/` (folder structure
`EBL000000/`, `EBL000001/`, ... underneath), also in `.gitignore`.

Then process the downloaded files as usual:

```bash
nmea2log Actisense/EBL000000/*.ebl Actisense/EBL000001/*.ebl -o logbook.csv
```

Or, simpler: run `nmea2log` with no arguments at all. It then searches the `ebl_dir` folder from
the config file (see "Config file for defaults" below) recursively for `.ebl` files — set once,
so after downloading you never have to select or drag files by hand again.

This EBL path is reverse-engineered (see "Assumptions & limitations") and has since been
validated against real SD card logs — when in doubt, always check that the outcome feels
plausible for your own boat/engine.

**Alternative**: capture the N2K ASCII stream from a Data Server to a file, for example by
running `nmea2log --live ... --tee 2026-07-15.raw` (see Option B), or with another terminal
program that writes the TCP stream to a file. Preferably name the file with a date in it, e.g.
`2026-07-15.raw` — that's used to correctly detect midnight rollovers (the time-of-day in the
format doesn't itself contain a date; `.ebl` files don't have this problem, they get their time
from the data itself).

```bash
nmea2log 2026-07-15.raw -o logbook.csv
```

This writes `logbook.csv`, `logbook.gpx` (the route per trip), and `logbook.html` (the
self-contained HTML logbook with totals, year/week grouping, and clickable maps).

Processing multiple files at once (e.g. one per day, `.ebl` and `.raw` mixed together):

```bash
nmea2log 2026-07-14.raw 2026-07-15.ebl 2026-07-16.raw -o logbook.csv
```

### Option B: live reading

Connect directly to the W2K-2 while sailing. Replace `192.168.4.1` with the W2K-2's IP address
on your network (found on the device's status page/web interface):

```bash
nmea2log --live 192.168.4.1 -o logbook.csv
```

The session keeps running until you press Ctrl+C (or `--duration` elapses); after that, the
logbook is written with everything that came in up to that point — a trip that hasn't yet been
closed off by a new port visit gets "Unknown (end outside log file)" as its arrival port. With
`--tee` you also keep the raw ASCII stream to a file at the same time, so you get both live
processing and a permanent log file:

```bash
nmea2log --live 192.168.4.1:60001 --tee 2026-07-16.raw -o logbook.csv
```

### Useful options

| Option | Meaning |
|---|---|
| `--live HOST[:PORT]` | Connect live to the W2K-2 over TCP instead of processing files (default port 60001) |
| `--tee PATH` | Only with `--live`: also save the raw incoming ASCII lines to this file |
| `--duration SECONDS` | Only with `--live`: stop automatically after this many seconds |
| `--speed-threshold-kn` | Speed (kn) below which the boat counts as 'stationary' (default 0.5) |
| `--min-stop-minutes` | Minimum stationary duration to count as a port visit (default 10) |
| `--max-gap-minutes` | From how many minutes without data a trip gets cut short (default: same as `--min-stop-minutes`) |
| `--min-trip-distance-nm` | Trips shorter than this are filtered out as noise instead of shown (default 0.1 nm) |
| `--no-geocode` | No internet needed; shows coordinates instead of port names |
| `--cache-file` | Path to the cache file for port names (default `.geocode_cache.json`) |
| `--start-date` | Force the start date of the first log file (`YYYY-MM-DD`); not applicable with `--live` |
| `--utc-offset HOURS` | Fixed timezone offset (e.g. `2` for CEST) for the displayed times. Default: automatically estimated per trip from the departure position |
| `--boat-name NAME` | Boat name at the top of the HTML logbook (default: none, or the `boat_name` setting from the config file) |
| `--engine-count N` | Number of physical engines. With `1`, any extra engine instance in the data is ignored as noise (same idea as the GPS source-dominance filtering) |
| `--ebl-dir DIR` | Folder to search recursively for `.ebl` files when no logfiles are given and `--live` isn't used either. Default: not set, or the `ebl_dir` setting from the config file |
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

### Config file for defaults

Instead of passing the options above on the command line every time, you can set them in the
`[nmea2log]` section of `nmea2log.ini` (see also "Option A: stored log files" above for the
`[w2k2]` section in the same file). Command-line arguments always override what's in the config
file. Example:

```ini
[nmea2log]
boat_name = Zeevalk
ebl_dir = Actisense
min_trip_distance_nm = 0.3
no_geocode = false
```

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
- **Line format (N2K ASCII)**: the parser is built from Actisense's official documentation on
  their website — the knowledge-base article
  ["NMEA 2000 ASCII Output format"](https://actisense.com/knowledge-base/nmea-2000/w2k-1-nmea-2000-to-wifi-gateway/nmea-2000-ascii-output-format/)
  and the [W2K-2 User Manual](https://actisense.com/products/w2k-2-nmea-2000-wifi-gateway/) (see
  the product page, downloads tab) — and the [canboat](https://github.com/canboat/canboat) PGN
  dictionary. I haven't been able to test this against a real log from your own W2K-2 — check
  the first few lines of a real log file against the regex in `ascii_reader.py` (`_LINE_RE`) and
  adjust it if it differs.
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
- **Multiple engines**: the code supports multiple `instance` numbers (fuel is summed, engine
  hours shown per engine separately), but hasn't been tested with a real twin-engine
  installation.
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
- **Date**: the N2K ASCII format only contains a time of day, no date. Make sure every log file
  has a date in its name (`YYYY-MM-DD...`), otherwise the file's modification date is used.
- **Water depth**: the app uses the raw "Depth" value from PGN 128267 (depth under the
  transducer), without adding the transducer offset — usually that's already the value
  instruments show by default, but check this against your own depth-sounder settings.
- **Engine warnings**: the bit meanings (e.g. "Low Oil Pressure") come from the generic NMEA2000
  standard list (canboat's ENGINE_STATUS_1/2). Some manufacturers use deviating or additional
  proprietary status bits for this — check this against your own engine documentation if a
  warning shows up unexpectedly or is missing.
- **Live mode (`--live`)** only supports **TCP** (the W2K-2 manual also recommends this because
  of built-in error correction; UDP-only isn't implemented). On a dropped connection, the session
  stops and the logbook is written with whatever came in up to that point — there's no automatic
  reconnect. The default port (60001) corresponds to "Data Server 1" on the W2K-2; check the
  device's web interface for which server is set to TCP + N2K ASCII and which port it uses.

## Working with Claude Code on this project

- Work in small, verifiable steps: run `pytest` after every change before moving on — the test
  layout above (`tests/`) is specifically there to make that fast.
- When adding new features, provide concrete example data (a snippet of real or realistic log
  lines), especially for anything involving PGN decoding — that saves guesswork.
- Once you have a real log file from the W2K-2: share a small fragment (a few hundred lines is
  enough) so the parsing format and PGN assumptions can be verified against real data.
