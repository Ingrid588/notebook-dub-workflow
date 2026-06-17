@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Gemini API Quick Setup

py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python 3.12 not found. Install it, then run: py -0p
    goto END
)

echo ==================================================
echo   Gemini API quick setup  (Key + model ONLY)
echo   Full options: config.bat
echo ==================================================
echo.

py -3.12 -u "%~dp0run_batch_course.py" --api-keys

:END
echo.
echo Done. Press any key to close...
pause >nul
