@echo off
setlocal
cd /d "%~dp0"

set "APP_DIR=%~dp0"
set "PYTHONW=%LocalAppData%\Programs\Python\Python311\pythonw.exe"

if exist "%PYTHONW%" (
    start "" "%PYTHONW%" "%APP_DIR%capswriter_gui.pyw"
    exit /b 0
)

where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3 "%APP_DIR%capswriter_gui.pyw"
    exit /b 0
)

where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe "%APP_DIR%capswriter_gui.pyw"
    exit /b 0
)

echo [CapsWriter] 未找到可用的 pythonw/pyw，请先安装 Python 3 并确保 pythonw.exe 可用。
pause
exit /b 1
