@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Analyze Subtitles

py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python 3.12 not found.
    goto END
)

echo ==================================================
echo   Analyze subtitles - long / short / merge
echo   Enter = all chapters, or type 1 / 1,3
echo ==================================================

py -3.12 -u "%~dp0analyze_subtitles.py"

:END
echo.
echo Done. Press any key to close this window...
pause >nul
