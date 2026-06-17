@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Apply Glossary to Subtitles

set "ROOT=%~dp0"
set "COMP=%ROOT%components"
set "PY=py -3.12"

set "WORKDIR="
if not "%~1"=="" (
    for %%I in ("%~1") do set "WORKDIR=%%~fI"
) else (
    set /p "WORKDIR=Folder path: "
)
if "!WORKDIR!"=="" goto END
for %%I in ("!WORKDIR!") do set "WORKDIR=%%~fI"

if not exist "!WORKDIR!\" (
    echo [Error] Folder not found
    exit /b 1
)

echo ==================================================
echo   Apply glossary to bilingual subtitles (zero API)
echo   Output: *.term-fixed.srt  (or --inplace)
echo ==================================================
echo.
echo [1] Preview only
echo [2] Save as *.term-fixed.srt  (recommended)
echo [3] Overwrite original merge/bilingual files
set /p "MODE=Choose 1/2/3 (Enter=2): "
if "!MODE!"=="1" (
    %PY% -u "%COMP%\apply_glossary_to_subs.py" "!WORKDIR!" --dry-run
) else if "!MODE!"=="3" (
    %PY% -u "%COMP%\apply_glossary_to_subs.py" "!WORKDIR!" --inplace
) else (
    %PY% -u "%COMP%\apply_glossary_to_subs.py" "!WORKDIR!"
)

:END
pause
endlocal
