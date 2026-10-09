@echo off
setlocal
chcp 65001 >nul
rem ============================================================
rem  重新打包安卓 APK（野人体系选股）
rem  用法：双击本文件，或在命令行执行 build_apk.bat
rem  产物：app\build\outputs\apk\debug\app-debug.apk
rem ============================================================

cd /d "%~dp0"

rem ---- 工具链位置（换机器改这里，或用环境变量覆盖）----
if "%ANDROID_TOOLCHAIN%"=="" set ANDROID_TOOLCHAIN=C:\Users\macro\android-toolchain
if "%JAVA_HOME%"=="" set JAVA_HOME=%ANDROID_TOOLCHAIN%\jdk17
if "%ANDROID_HOME%"=="" set ANDROID_HOME=%ANDROID_TOOLCHAIN%\sdk
set GRADLE=%ANDROID_TOOLCHAIN%\gradle-8.7\bin\gradle.bat

echo.
echo [1/3] 检查工具链
if not exist "%JAVA_HOME%\bin\java.exe"  ( echo   X 找不到 JDK:    %JAVA_HOME% & goto :err )
if not exist "%ANDROID_HOME%\platforms"  ( echo   X 找不到 SDK:    %ANDROID_HOME% & goto :err )
if not exist "%GRADLE%"                   ( echo   X 找不到 Gradle: %GRADLE% & goto :err )
echo   OK JDK    %JAVA_HOME%
echo   OK SDK    %ANDROID_HOME%
echo   OK Gradle %GRADLE%

echo.
echo [2/3] 编译（会自动把 ..\ 的 Python 代码和前端页面同步进来）
call "%GRADLE%" :app:assembleDebug --console=plain
if errorlevel 1 goto :err

echo.
echo [3/3] 完成
set APK=app\build\outputs\apk\debug\app-debug.apk
if not exist "%APK%" ( echo   X 没有生成 APK & goto :err )
for %%A in ("%APK%") do echo   产物: %%~fA  (%%~zA 字节)
echo.
echo 安装到手机（需开启 USB 调试）:
echo    adb install -r "%APK%"
echo 或者把 apk 拷到手机里直接点安装。
exit /b 0

:err
echo.
echo *** 构建失败 ***
exit /b 1
