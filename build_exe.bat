@echo off
REM ============================================================
REM  打包桌面版独立窗口 exe  ->  dist\野人看板.exe
REM  依赖（一次性）：pip install pyinstaller pywebview
REM  说明：单文件 --windowed（无控制台）；数据/缓存落在 exe 同级的
REM        data\ 与 cache\；web\ 随包分发，mootdx 一并打进去。
REM ============================================================
setlocal
cd /d "%~dp0"
set "PY=C:\Users\macro\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name "野人看板" ^
  --add-data "%~dp0web;web" ^
  --collect-submodules core --collect-all mootdx ^
  --hidden-import launcher --hidden-import server ^
  --exclude-module tkinter ^
  --distpath "%~dp0dist" --workpath "%~dp0build" --specpath "%~dp0build" ^
  "%~dp0desktop_app.py"

echo.
echo   完成：dist\野人看板.exe   （双击即开独立窗口，不再走浏览器）
pause
