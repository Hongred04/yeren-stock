@echo off
setlocal
chcp 65001 >nul 2>&1

set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\StockMonitor.lnk"

echo ==============================================================
echo   Disable auto-start
echo ==============================================================
echo.

if exist "%LNK%" (
    del /q "%LNK%"
    echo   [OK] Removed: %LNK%
    echo.
    echo   It will no longer start at logon.
) else (
    echo   [INFO] Auto-start shortcut not found. Nothing to do.
)

echo.
echo   Note: if a service is already running, close its console window
echo   or run  stop.bat
echo.
pause
