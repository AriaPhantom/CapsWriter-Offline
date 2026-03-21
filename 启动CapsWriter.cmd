@echo off
setlocal
cd /d "%~dp0"

set "SCRIPT=%~dp0capswriter_gui.pyw"
set "PYTHONW=%LocalAppData%\Programs\Python\Python311\pythonw.exe"

if exist "%PYTHONW%" goto run_pythonw

where pythonw.exe >nul 2>nul
if not errorlevel 1 goto run_path_pythonw

where pyw.exe >nul 2>nul
if not errorlevel 1 goto run_pyw

echo [CapsWriter] No usable pythonw/pyw was found. Please install Python 3 first.
pause
exit /b 1

:run_pythonw
start "" "%PYTHONW%" "%SCRIPT%"
exit /b 0

:run_path_pythonw
start "" pythonw.exe "%SCRIPT%"
exit /b 0

:run_pyw
start "" pyw.exe -3 "%SCRIPT%"
exit /b 0
