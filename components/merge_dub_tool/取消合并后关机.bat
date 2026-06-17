@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Cancel Shutdown After Merge

py -3.12 --version >nul 2>&1
if errorlevel 1 (
    set "PY=py -3"
) else (
    set "PY=py -3.12"
)

%PY% -u "%~dp0merge_dub_videos.py" --cancel-shutdown

echo.
pause
