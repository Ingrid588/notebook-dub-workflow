@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Commercial Dub Test

set "ROOT=%~dp0"
set "COMP=%ROOT%components"
set "PY=py -3.12"

%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python 3.12 not found. Run: py -0p
    goto END
)

ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [Warn] ffmpeg not found - video merge may fail
    echo.
)

echo ==================================================
echo   Commercial Dub Test  (Route B step 2: NO Gemini here)
echo   Step 1 Gemini: fit_translate.bat  -^>  fit-dub.srt
echo   This bat: TTS + merge only (dub_translate_once)
echo   Input: folder with mp4 + bilingual or fit-dub.srt
echo   Settings: components\config.bat
echo   API Key:  GEMINI_API_KEY (see GEMINI_API_KEY_SETUP.txt)
echo.
echo   Route A learn experiment: quick_bilingual_dub.bat
echo   See WORKFLOWS.txt
echo ==================================================
echo.

set "WORKDIR="

if not "%~1"=="" (
    for %%I in ("%~1") do set "WORKDIR=%%~fI"
    echo [Selected] !WORKDIR!
    goto RUN
)

echo Usage:
echo   1. Drag a folder onto this bat
echo   2. Or type folder path below
echo.

set /p "WORKDIR=Folder path: "
if "%WORKDIR%"=="" goto END
for %%I in ("%WORKDIR%") do set "WORKDIR=%%~fI"

:RUN
if not exist "%WORKDIR%\" (
    echo [Error] Folder not found: %WORKDIR%
    goto END
)

echo.
echo [1/2] Checking packages...
%PY% -u "%COMP%\check_env.py"
if errorlevel 2 (
    echo Installing packages...
    %PY% -m pip install -r "%COMP%\requirements.txt"
    if errorlevel 1 goto END
    %PY% -u "%COMP%\check_env.py"
)
if errorlevel 1 goto END

echo.
echo [2/2] Commercial dubbing (run_batch_course.py --work-dir)...
echo       No Gemini in this step if dub_translate_once=true
echo.
set "TUBEGET_TRANSLATE_ONCE=1"
%PY% -u "%COMP%\run_batch_course.py" --work-dir "%WORKDIR%" --no-dialog
set "ERR=%ERRORLEVEL%"

if "%ERR%" neq "0" (
    echo.
    echo [Hint] See *_C.gemini-run.txt / *.dub-status.json in video folder
)

:END
echo.
echo Done. Press any key to close...
pause >nul
endlocal
