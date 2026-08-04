@echo off
setlocal

if "%~1"=="" (
    set "OUTPUT=%~dp0logbook.csv"
) else (
    set "OUTPUT=%~dp1logbook.csv"
)

py -m nmea2000processor %* -o "%OUTPUT%"

echo.
echo Done. Press any key to close this window.
pause >nul
