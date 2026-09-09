@echo off
setlocal

rem Without this, relative-path state files (the .ebl sample cache, .geocode_cache.json,
rem nmea2log.ini itself) resolve against whatever directory happened to be current when this .bat
rem was launched from -- not necessarily this folder, depending on how it was started
rem (double-click vs. a shortcut with a different "Start in" folder vs. drag-and-drop).
cd /d "%~dp0"

rem Comment out this line to stop opening a browser after each run.
set "VIEW_URL=https://ayuus.com/little_endian/"

if "%~1"=="" (
    set "OUTPUT=%~dp0logbook.csv"
    py -m nmea2000processor.w2k2_download
    if errorlevel 1 (
        echo.
        echo Download failed or the boat wasn't reachable -- continuing with whatever is
        echo already downloaded. Use nmea2log-no-download.bat to skip this step entirely.
    )
) else (
    set "OUTPUT=%~dp1logbook.csv"
)

py -m nmea2000processor %* -o "%OUTPUT%"
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