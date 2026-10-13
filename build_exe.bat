@echo off
REM ============================================================
REM  打包桌面版独立窗口 exe  ->  dist\野人看板.exe
REM  依赖（一次性）：pip install pyinstaller pywebview
REM  说明：单文件 --windowed（无控制台）；数据/缓存落在 exe 同级的
REM        data\ 与 cache\；web\ 随包分发，mootdx 一并打进去。
REM ============================================================
setlocal
cd /d "%~dp0"
REM  Pick the interpreter that has mootdx installed: --collect-all mootdx
REM  needs it present in the BUILD environment, otherwise the packaged exe
REM  silently ships without the TDX source (falls back to Tencent/Sina and
REM  never says so).
REM  NOTE: keep every *echoed* line ASCII-only. This file is UTF-8 and the
REM  console is GBK, so echoing Chinese here prints mojibake.
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "C:\Users\macro\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe" (
    set "PY=C:\Users\macro\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"
)
if not defined PY if exist "C:\Users\macro\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
    set "PY=C:\Users\macro\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)
if not defined PY set "PY=python"

"%PY%" -c "import mootdx" >nul 2>&1 || (
    echo [WARN] This interpreter has no mootdx - the exe will ship WITHOUT
    echo        the TDX source. Install it first:
    echo          "%PY%" -m pip install mootdx
    echo.
)

"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name "野人看板" ^
  --add-data "%~dp0web;web" ^
  --collect-submodules core --collect-all mootdx ^
  --hidden-import launcher --hidden-import server ^
  --exclude-module tkinter ^
  --distpath "%~dp0dist" --workpath "%~dp0build" --specpath "%~dp0build" ^
  "%~dp0desktop_app.py"

echo.
REM  Keep echoed lines ASCII: this file is UTF-8 while the console is GBK,
REM  so echoing Chinese here prints mojibake. The exe NAME (--name below) is
REM  the one place that must stay Chinese; it is passed as an argument, never
REM  echoed, so it does not go through this path.
echo   Done. Output is in the dist\ folder - double-click the exe to open
echo   a standalone window (no browser).
pause
