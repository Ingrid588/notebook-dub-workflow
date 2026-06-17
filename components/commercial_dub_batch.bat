@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Commercial Dub Batch

py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python 3.12 not found.
    goto END
)

echo ==================================================
echo   Commercial Dub Batch  (Route B: Gemini API)
echo   Full pipeline: run_batch_course.py
echo   Needs: *.bilingual.srt per video under components
echo   NOT for NotebookLM learn experiment (use quick_bilingual_dub)
echo   Single folder test: ..\commercial_dub_test.bat
echo ==================================================
echo.

echo [1/2] Checking packages...
py -3.12 -u "%~dp0check_env.py"
if errorlevel 2 (
    echo Installing packages...
    py -3.12 -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 goto END
    py -3.12 -u "%~dp0check_env.py"
)
if errorlevel 1 goto END

echo.
echo [2/2] Starting commercial batch dubbing...
echo   Enter chapter e.g. 1 or 1,3 ; Enter=all
echo   Policy: run fit_translate first; no Gemini in this step
echo.
set TUBEGET_TRANSLATE_ONCE=1
py -3.12 -u "%~dp0run_batch_course.py" --no-dialog

:END
echo.
echo Done. Press any key to close...
pause >nul

