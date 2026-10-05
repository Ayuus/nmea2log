# The log file (`nmea2log.log`)

Everything the app and the desktop command report while they work ends up in one plain-text file,
`nmea2log.log`. It is the place to look when something went wrong and the log view on screen has long
since scrolled away or been closed. This page says what is in it, where it is on each platform, how long
lines are kept, and how big it can get.

## What is in it

One line per message, UTF-8, no rotation:

```
2026-10-05 08:58:18 [info] Boot-modus: zoeken naar de W2K-2...
2026-10-05 08:58:19 [warning] ...
```

`YYYY-MM-DD HH:MM:SS`, a tag (`[info]`, `[ok]`, `[skip]`, `[cache]`, `[warning]`, `[error]`, `[geocode]`,
`[anomaly]`, ...), then the text. A message of several lines gets a timestamp on every line.

The file receives **every** log level, not only what is shown on screen. The pipeline knows two levels:
`info` (what the log view and the desktop console show) and `debug` (routine per-item chatter that is only
useful when troubleshooting). The debug lines are written to the file regardless; they are not shown in the
app's log view, and on the desktop they are only printed with `-v/--verbose`. There is no setting in the
apps to show them on screen -- the file is where they are.

What is written:

- everything `nmea2log` logs during a download, import, assemble or publish (the same lines the log view shows,
  plus the debug ones);
- on a failure with an unexpected exception: the full Python traceback, one line each, tagged `[error]`;
- the lines the apps make themselves ("Samenstellen geannuleerd.", settings saved, the boat mode's status
  lines, ...). Both apps stamp them like Python's own lines and append them to the same file.

Not in it: the contents of the logbook, and no password or token is written by the code (a login only logs
"logged in, token received").

## Where it is

| Platform | Location | How to get at it |
|---|---|---|
| Desktop (`nmea2log`) | `nmea2log.log` next to the output file (`--output`) | open it |
| iOS | `Documents/nmea2log.log` in the app's folder | the **Files** app (On My iPhone > My Sailing Logbook), or Finder over USB (the app enables file sharing) |
| Android | `nmea2log.log` in the app's external files folder, next to the `Actisense` folder: `Android/data/com.ayuus.mysailinglogbook/files/nmea2log.log` | connect the phone to a PC (file transfer / MTP) and open that folder. Earlier versions kept it in private storage; the first start of a newer version moves those lines into the new file |

The log *view* in the app is a separate thing: a list in memory (Android and iOS) that keeps the last
200,000 lines of the run and is empty again after the app process is restarted. The file is the history.

## How long lines are kept

`DEFAULT_LOG_RETENTION_DAYS = 30`. Lines older than that are dropped when the next **run starts** (a
download, an assemble, a boat-mode round: whatever calls `set_log_file()` -- in the apps that is
`android_entry.sync_from_w2k2()` and `build_from_local_files()`). Nothing trims the file in between, so
lines older than 30 days can still sit in it until the next run. On the desktop the period is
`--log-retention-days`. A line that does not start with a timestamp is never dropped.

## How big it can get

There is **no size limit**; only the 30-day age limit applies. Rough numbers (measured on the desktop log and a
tablet, otherwise worked out from the code):

- Desktop, a few weeks of normal use: ~220 KB (2,600 lines, about 85 bytes per line).
- Android tablet, 9 days of testing with several full rebuilds and interrupted runs: 1.1 MB (16,000 lines).
- An assemble with everything cached: tens to a few hundred lines (a few KB). A full rebuild from scratch of
  an archive of ~2,300 files: a few thousand lines, roughly 0.2-0.4 MB (more on a slow phone or tablet, which
  logs a progress line per file).
- **A download or boat-mode round against the W2K-2 writes one debug line per file the W2K-2 holds**
  (`[skip] EBL000012/000012_007.ebl already complete locally`, about 85 bytes), also for files that are
  already on the device: `android_entry.sync_from_w2k2()` walks the whole download plan and
  `w2k2_download.download_file()` logs the skip. With ~2,300 files that is about **0.2 MB per run**.

The last point is what can make the file large: the boat mode does a round every 30 minutes to a few
hours while it is on. At one round per hour that is ~5 MB per day, so on the order of 150 MB when it
runs for the whole 30 days; a few manual syncs a week stay in the single MB. (Worked out from the code, not
measured with boat mode on for weeks.) If the file ever needs to stay small, the options are to log the skipped files as a
single summary line, to leave the debug lines out of the file, or to cap its size -- none of which is done.

## Where this is implemented

- `src/nmea2log/log.py`: `log()`, `set_log_file()` (append mode, `_prune_old_lines()`), `set_log_level()`,
  `log_exception()`, `stamp_line()`.
- `src/nmea2log/app_constants.py`: `LOG_FILE_NAME`, the tags that are coloured red/yellow, the log view's limits.
- Android: `LogFile.kt` (where the file is, and the move from private storage), `AppLog.kt` (writes the app's own lines), `LogBuffer.kt`/`LogAdapter.kt` (the log view); `android_entry.set_log_directory()` tells Python the folder.
- iOS: `MySailingLogbook.log()` and `_append_to_log_file()` in `app.py`.
