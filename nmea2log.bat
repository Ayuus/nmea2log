@echo off
setlocal

if "%~1"=="" (
    echo Sleep een .raw-logbestand op deze snelkoppeling om het te verwerken.
    echo.
    echo Of gebruik handmatig:
    echo   nmea2log.bat pad\naar\logbestand.raw
    pause
    exit /b 1
)

set "OUTPUT=%~dp1logboek.csv"

"C:\Users\ruijs\AppData\Local\Programs\Python\Python313\Scripts\nmea2log.exe" %* -o "%OUTPUT%"

echo.
echo Klaar. Druk op een toets om dit venster te sluiten.
pause >nul
