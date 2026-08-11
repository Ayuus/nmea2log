@echo off
setlocal

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
