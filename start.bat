@echo off
setlocal
cd /d "%~dp0"

title Stock Monitor - Intelligent A-Share Trading System
chcp 65001 >nul 2>&1

echo ==============================================================
echo   Stock Monitor - Intelligent A-Share Trading System
echo   Strategy engine: 8-module decision chain
echo ==============================================================
echo.

set "PY="
if exist "C:\Users\macro\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
    set "PY=C:\Users\macro\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)

if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PY set "PY=%%P"
    )
)

if not defined PY (
    echo [ERROR] Python interpreter not found.
    echo.
    echo   Expected one of:
    echo     C:\Users\macro\.workbuddy\binaries\python\versions\3.13.12\python.exe
    echo     python on PATH
    echo.
    echo   Open WorkBuddy and ask it to repair the launcher.
    echo.
    pause
    exit /b 1
)

echo   Interpreter: %PY%
echo   Working dir: %CD%
echo.

"%PY%" launcher.py %*

echo.
echo   Service stopped.
pause
