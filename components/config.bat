@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Dubbing and Gemini Settings

py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python 3.12 not found. Install it, then run: py -0p
    goto END
)

echo ==================================================
echo   Dubbing + Gemini Settings  (run BEFORE batch)
echo   API Key: Windows user env GEMINI_API_KEY only
echo   Setup:   components\GEMINI_API_KEY_SETUP.txt
echo ==================================================
echo.

py -3.12 -u "%~dp0run_batch_course.py" --config

:END
echo.
echo Next: set GEMINI_API_KEY, then commercial_dub_test.bat  (Route B)
echo       Learn experiment: quick_bilingual_dub.bat  (Route A, see WORKFLOWS.txt)
echo        See components\GEMINI_API_KEY_SETUP.txt
echo.
echo Done. Press any key to close...
pause >nul
