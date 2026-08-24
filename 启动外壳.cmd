@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "SHELL_EXE=shell\src-tauri\target\release\capswriter-shell.exe"

if not exist "%SHELL_EXE%" (
    echo [错误] 外壳尚未构建。
    echo.
    echo 请先构建：
    echo     cd shell
    echo     npm install
    echo     npm run tauri build
    echo.
    pause
    exit /b 1
)

REM 已在运行就不重复启动（外壳自身常驻托盘）
tasklist /FI "IMAGENAME eq capswriter-shell.exe" 2>nul | find /I "capswriter-shell.exe" >nul
if not errorlevel 1 (
    echo 外壳已在运行，可从系统托盘打开面板。
    timeout /t 2 >nul
    exit /b 0
)

if not exist logs mkdir logs
start "" "%SHELL_EXE%"
exit /b 0
