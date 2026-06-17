@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Merge Video + Dub Audio

py -3.12 --version >nul 2>&1
if errorlevel 1 (
    set "PY=py -3"
) else (
    set "PY=py -3.12"
)

ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] ffmpeg not found. Install and add to PATH.
    echo         https://ffmpeg.org/download.html
    goto END
)

%PY% -u "%~dp0merge_dub_videos.py" --menu

:END
echo.
pause
