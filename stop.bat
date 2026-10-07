@echo off
setlocal
chcp 65001 >nul 2>&1

echo ==============================================================
echo   Stop any running Stock Monitor service
echo ==============================================================
echo.

for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8777" ^| findstr "LISTENING"') do (
    echo   Killing PID %%P on port 8777...
    taskkill /F /PID %%P >nul 2>&1
)

echo.
echo   Done. Port 8777 released.
echo.
pause
