@echo off
setlocal

if "%~1"=="" (
    echo Drag a .raw log file onto this shortcut to process it.
    echo.
    echo Or use it manually:
    echo   nmea2log.bat path\to\logfile.raw
    pause
    exit /b 1
)

set "OUTPUT=%~dp1logbook.csv"

py -m nmea2000processor %* -o "%OUTPUT%"

echo.
echo Done. Press any key to close this window.
pause >nul
