@echo off
setlocal

rem Without this, relative-path state files (trip_ids.json, the .ebl sample cache,
rem .geocode_cache.json, nmea2log.ini itself) resolve against whatever directory happened to be
rem current when this .bat was launched from -- not necessarily this folder, depending on how it
rem was started (double-click vs. a shortcut with a different "Start in" folder vs. drag-and-drop)
rem -- silently switching to a different/empty trip_ids.json between runs and orphaning every
rem trip's uid, and with it any remark already saved against the old one (found in practice).
cd /d "%~dp0"

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

start "" "%OUTPUT:.csv=.html%"
