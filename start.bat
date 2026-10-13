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

REM ---------------------------------------------------------------
REM  KEEP THIS FILE ASCII-ONLY. It runs `chcp 65001` below; if any
REM  multi-byte character appears after that, cmd.exe mis-seeks the
REM  batch file (it tracks byte offsets in the OLD code page), slices
REM  a UTF-8 sequence in half, and tries to run the garbage as a
REM  command -> "'...' is not recognized as an internal or external
REM  command". Chinese output is printed by launcher.py / server.py
REM  instead (they run after chcp, via the console API).
REM ---------------------------------------------------------------

REM  Pick the interpreter: prefer a venv that has the optional deps.
REM  mootdx is optional - with it, daily/index K-line uses the TDX TCP
REM  source (port 7709, not IP-blocked); without it we fall back to
REM  Tencent/Sina.
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "C:\Users\macro\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe" (
    set "PY=C:\Users\macro\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"
)
if not defined PY if exist "C:\Users\macro\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
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
    echo     .venv\Scripts\python.exe
    echo     C:\Users\macro\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe
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

REM  Report whether the optional TDX source is usable with this interpreter.
"%PY%" -c "import mootdx" >nul 2>&1
if errorlevel 1 (
    echo   TDX source:  not installed - K-line falls back to Tencent/Sina
) else (
    echo   TDX source:  available ^(mootdx installed^)
)
echo.

"%PY%" launcher.py %*

echo.
echo   Service stopped.
pause
