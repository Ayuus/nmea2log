@echo off
setlocal

rem Without this, relative-path state files (the .ebl sample cache, .geocode_cache.json,
rem nmea2log.ini itself) resolve against whatever directory happened to be current when this .bat
rem was launched from -- not necessarily this folder, depending on how it was started
rem (double-click vs. a shortcut with a different "Start in" folder).
cd /d "%~dp0"

rem Optional local override, e.g. "set VIEW_URL=https://your-site/your-slug/" to open a browser
rem there after each run -- see nmea2log-local.bat.example. Not in git (.gitignore): unlike the
rem rest of this script, a real logbook URL is specific to your own site, not something to commit.
if exist "%~dp0nmea2log-local.bat" call "%~dp0nmea2log-local.bat"

set "OUTPUT=%~dp0logbook.csv"
py -m nmea2log.w2k2_download
if errorlevel 1 (
    echo.
    echo Download failed or the boat wasn't reachable -- continuing with whatever is
    echo already downloaded.
)

py -m nmea2log -o "%OUTPUT%"
if errorlevel 1 (
    echo.
    echo Something went wrong -- see the message above.
    pause
    exit /b 1
)

if defined VIEW_URL (
    start "" "%VIEW_URL%"
) else (
    start "" "%OUTPUT:.csv=.html%"
)
