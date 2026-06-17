@echo off
setlocal EnableExtensions
cd /d "%~dp0components"
title Dubbing and Gemini Settings

py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python 3.12 not found. Install it, then run: py -0p
    goto END
)

echo ==================================================
echo   Dubbing + Gemini Settings  (run this BEFORE batch)
echo   Opens full dialog: subtitle opt, Speech Activity,
echo   Gemini scope (all cues / problem cues only), API.
echo ==================================================
echo   Folder: %CD%
echo.

py -3.12 -u run_batch_course.py --config

:END
echo.
echo Next: run batch dubbing bat in components folder.
echo.
echo Done. Press any key to close...
pause >nul
