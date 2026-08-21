@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "%CD%\app.py" (
    echo SISU files were not found in:
    echo   %CD%
    pause
    exit /b 1
)

where pyw >nul 2>&1
if not errorlevel 1 (
    start "SISU" /D "%CD%" pyw.exe -3 "%CD%\app.py"
    exit /b 0
)
where pythonw >nul 2>&1
if not errorlevel 1 (
    start "SISU" /D "%CD%" pythonw.exe "%CD%\app.py"
    exit /b 0
)
where py >nul 2>&1
if not errorlevel 1 (
    py -3 "%CD%\app.py"
    if errorlevel 1 pause
    exit /b %ERRORLEVEL%
)
python "%CD%\app.py"
if errorlevel 1 pause
exit /b %ERRORLEVEL%
