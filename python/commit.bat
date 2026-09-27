@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul

if /I "%~1"=="-h" goto usage
if /I "%~1"=="--help" goto usage
if /I "%~1"=="/?" goto usage

if not exist "%CD%\app.py" (
    echo SISU files were not found in:
    echo   %CD%
    exit /b 1
)

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo This folder is not a Git repository, so it cannot be committed.
    exit /b 1
)

set "PUSH=0"
set "MSG="

:parse
if "%~1"=="" goto parsed
if /I "%~1"=="-p" (
    set "PUSH=1"
    shift
    goto parse
)
if /I "%~1"=="/p" (
    set "PUSH=1"
    shift
    goto parse
)
if /I "%~1"=="--push" (
    set "PUSH=1"
    shift
    goto parse
)
if /I "%~1"=="-h" goto usage
if /I "%~1"=="--help" goto usage
if not defined MSG (
    set "MSG=%~1"
) else (
    set "MSG=!MSG! %~1"
)
shift
goto parse

:parsed
echo.
echo  SISU commit
echo  -----------
echo  Folder: %CD%
echo.

if defined MSG (
    echo Draft commit message:
    echo   !MSG!
    echo.
    set /p "FINAL=Final commit message [Enter keeps draft]: "
) else (
    set /p "FINAL=Commit message: "
)
if not defined FINAL set "FINAL=!MSG!"
if not defined FINAL (
    echo No commit message. Cancelled.
    exit /b 1
)

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

echo.
echo Bumping version ...
set "NEW_VER="
set "NEW_STAMP="
for /f "usebackq tokens=1,2 delims=|" %%A in (`%PY% -c "from app_update import bump_app_version; v=bump_app_version(); print(str(v.number)+'|'+v.date)"`) do (
    set "NEW_VER=%%A"
    set "NEW_STAMP=%%B"
)
if not defined NEW_VER (
    echo Could not increase the version.
    exit /b 1
)
echo Version !NEW_VER!  !NEW_STAMP!

set "MSGFILE=%TEMP%\sisu-commit-msg.txt"
set "SISU_COMMIT_MSG=!FINAL!"
set "SISU_MSG_FILE=!MSGFILE!"
%PY% -c "import os; from pathlib import Path; Path(os.environ['SISU_MSG_FILE']).write_text(os.environ['SISU_COMMIT_MSG'].rstrip()+'\n', encoding='utf-8')"
if errorlevel 1 (
    echo Could not write the commit message file.
    exit /b 1
)

echo.
git add -A
if errorlevel 1 (
    echo git add failed.
    exit /b 1
)

echo Committing ...
git commit -F "!MSGFILE!"
set "COMMIT_ERR=!ERRORLEVEL!"
del /q "!MSGFILE!" >nul 2>&1
if not "!COMMIT_ERR!"=="0" (
    echo Commit failed. version.json was already bumped to !NEW_VER!.
    exit /b !COMMIT_ERR!
)

echo.
echo Committed v!NEW_VER! · !NEW_STAMP!
if "!PUSH!"=="1" (
    echo Pushing ...
    git push
    if errorlevel 1 (
        echo Commit succeeded, but git push failed.
        exit /b 1
    )
    echo Pushed.
) else (
    echo Not pushed. Pass -p to push.
)
echo.
exit /b 0

:usage
echo Usage: commit.bat [-p] [message]
echo.
echo   message   Optional draft commit message. You are prompted to confirm or edit it.
echo   -p        Push after a successful commit. Without -p, the commit stays local.
echo.
echo Examples:
echo   commit.bat "Site URL checkboxes"
echo   commit.bat -p "Site URL checkboxes"
echo   commit.bat
exit /b 0
