@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title TubeGet Batch

where python >nul 2>&1
py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python 3.12 not found. Install it, then run: py -0p
    goto :END
)

set "PY=py -3.12"

echo ==================================================
echo   TubeGet - Batch Process
echo   Scanning subfolders...
echo ==================================================
echo.

set FOUND=0

REM Process subfolders
for /d %%d in (*) do (
    if exist "%%d\*.mp4" (
        set FOUND=1
        echo --------------------------------------------------
        echo   Folder: %%d
        echo --------------------------------------------------
        pushd "%%d"
        %PY% -u "%~dp0run_batch.py"
        popd
        echo.
    )
)

REM Also process current directory if it has videos
dir /b *.mp4 >nul 2>&1
if not errorlevel 1 (
    set FOUND=1
    echo --------------------------------------------------
    echo   Folder: current
    echo --------------------------------------------------
    %PY% -u "%~dp0run_batch.py"
)

if %FOUND%==0 (
    echo   No video files found in any subfolder.
)

:END
echo.
echo Press any key to close...
pause >nul
