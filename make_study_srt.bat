@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Make study .srt from bilingual

set "ROOT=%~dp0"
set "COMP=%ROOT%components"
set "PY=py -3.12"
set "ERR=0"

set "WORKDIR="
if not "%~1"=="" (
    for %%I in ("%~1") do set "WORKDIR=%%~fI"
    goto RUN
)

echo Drag folder onto this bat, or paste path:
set /p "WORKDIR=Folder: "
if "!WORKDIR!"=="" goto END
for %%I in ("!WORKDIR!") do set "WORKDIR=%%~fI"

:RUN
if not exist "!WORKDIR!\" (
    echo [Error] Not found: !WORKDIR!
    set "ERR=1"
    goto END
)

echo ==================================================
echo   Generate video-name.srt from *.bilingual.srt
echo   Folder: !WORKDIR!
echo ==================================================
echo.

%PY% -u "%COMP%\make_study_srt.py" "!WORKDIR!"
set "ERR=!ERRORLEVEL!"

:END
echo.
pause
endlocal
exit /b %ERR%
