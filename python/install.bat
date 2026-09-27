@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo  SISU update
echo  -----------
echo  Folder: %CD%
echo.

set "FROM_START=0"
if /I "%~1"=="--from-start" set "FROM_START=1"

set "PY="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY set "PY=python"

echo Pulling the latest SISU files ...
set "GIT_TERMINAL_PROMPT=0"
git fetch --prune
if errorlevel 1 (
    echo Could not reach GitHub. Using the files already in this folder.
) else (
    git pull --ff-only
    if errorlevel 1 (
        echo Could not apply the update automatically. Using the files already in this folder.
    )
)

echo.
echo Installing any new libraries ...
%PY% -m pip install -r "%CD%\requirements.txt"
if errorlevel 1 (
    echo Could not install the required packages.
    if "%FROM_START%"=="0" pause
    exit /b 1
)

echo.
echo Creating the desktop icon ...
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%CD%\setup_desktop.ps1" -Repo "%CD%"
if errorlevel 1 (
    echo Could not create the desktop shortcut. You can start SISU with start.bat in this folder.
) else (
    echo Desktop shortcut "SISU" is ready.
)

echo.
echo Done. Use the desktop SISU icon to start.
echo Later updates are offered inside the app, then SISU restarts itself.
echo.
if "%FROM_START%"=="0" pause
exit /b 0
