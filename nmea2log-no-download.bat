@echo off
setlocal

if "%~1"=="" (
    set "OUTPUT=%~dp0logbook.csv"
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
