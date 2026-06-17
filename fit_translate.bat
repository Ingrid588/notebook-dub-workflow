@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Fit Dub Translate (Gemini API)

set "ROOT=%~dp0"
set "COMP=%ROOT%components"
set "PY=py -3.12"

%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python 3.12 not found.
    goto END
)

echo ==================================================
echo   Fit Dub Translate  (translate INTO time window)
echo   Replaces manual NotebookLM for batch automation
echo   Needs: GEMINI_API_KEY in Windows user env
echo   Input:  folder with *bilingual*.srt + mp4
echo   Output: *.fit-dub.srt  (then quick_bilingual_dub)
echo   Prompt: components\notebooklm_dub_prompt.txt
echo ==================================================
echo.

set "WORKDIR="

if not "%~1"=="" (
    for %%I in ("%~1") do set "WORKDIR=%%~fI"
    goto RUN
)

echo Drag folder onto this bat, or paste path:
set /p "WORKDIR=Folder path: "
if "!WORKDIR!"=="" goto END
for %%I in ("!WORKDIR!") do set "WORKDIR=%%~fI"

:RUN
if not exist "!WORKDIR!" (
    echo [Error] Not found: !WORKDIR!
    goto END
)

echo.
%PY% -u "%COMP%\fit_translate_bilingual.py" "!WORKDIR!"
set "ERR=!ERRORLEVEL!"

:END
echo.
if "!ERR!" neq "0" (
    echo Done with errors. Check GEMINI_API_KEY.
) else (
    echo Done. Route B next: commercial_dub_test.bat on same folder.
)
echo.
pause >nul
endlocal
