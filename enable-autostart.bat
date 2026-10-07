@echo off
setlocal
chcp 65001 >nul 2>&1

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK=%STARTUP%\StockMonitor.lnk"
set "PYW=C:\Users\macro\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
set "TARGET=%~dp0launcher.py"

echo ==============================================================
echo   Enable auto-start at Windows logon
echo ==============================================================
echo.

if not exist "%PYW%" (
    echo [ERROR] pythonw.exe not found:
    echo         %PYW%
    echo.
    pause
    exit /b 1
)

if not exist "%STARTUP%" (
    echo [ERROR] Startup folder not found:
    echo         %STARTUP%
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=New-Object -ComObject WScript.Shell;" ^
  "$l=$s.CreateShortcut('%LNK%');" ^
  "$l.TargetPath='%PYW%';" ^
  "$l.Arguments='\"%TARGET%\" --no-browser';" ^
  "$l.WorkingDirectory='%~dp0';" ^
  "$l.WindowStyle=7;" ^
  "$l.Description='Stock Monitor - silent dashboard service';" ^
  "$l.Save();"

if exist "%LNK%" (
    echo   [OK] Auto-start enabled.
    echo.
    echo   Shortcut: %LNK%
    echo.
    echo   The service now starts silently at logon with no console window
    echo   and no browser. Open the dashboard manually at:
    echo       http://127.0.0.1:8777
    echo.
    echo   First logon will need to re-download kline data (no cache yet),
    echo   which takes a while. It runs in the background.
    echo.
    echo   To disable: double-click  disable-autostart.bat
) else (
    echo   [FAILED] Could not create the shortcut.
    echo   Try running this file as administrator.
)

echo.
pause
