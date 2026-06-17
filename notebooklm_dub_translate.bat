@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title NotebookLM Auto Translate + Dub

set "ROOT=%~dp0"
set "COMP=%ROOT%components"
set "PY=py -3.12"
set "ERR=0"
set "STAGE=init"
set "DODUB=1"
set "LESSON_ARGS="

if /i "%~1"=="--no-dub" (
    set "DODUB=0"
    shift
)
if /i "%~1"=="--dub" (
    set "DODUB=1"
    shift
)

set "WORKDIR="
if not "%~1"=="" (
    for %%I in ("%~1") do set "WORKDIR=%%~fI"
    shift
    goto PANEL
)

echo Drag folder onto this bat, or paste path:
set /p "WORKDIR=Folder: "
if "!WORKDIR!"=="" goto END
for %%I in ("!WORKDIR!") do set "WORKDIR=%%~fI"

:PANEL
if not exist "!WORKDIR!\" (
    echo [Error] Not found: !WORKDIR!
    set "ERR=1"
    goto END
)

echo ==================================================
echo   NotebookLM full auto: deps + login + translate + dub
echo   Folder: !WORKDIR!
if "!DODUB!"=="0" echo   Mode: translate only (--no-dub)
if "!DODUB!"=="1" echo   Mode: translate then quick_bilingual_dub
echo ==================================================
echo.

echo [0/3] Check deps and NotebookLM login (browser only if needed) ...
set "STAGE=ensure_ready"
%PY% -u "%COMP%\notebooklm_dub_automation.py" --ensure-ready
if errorlevel 1 (
    set "ERR=1"
    goto END
)
echo.

echo [1/3] Lesson numbers in folder
set "STAGE=list_lessons"
%PY% -u "%COMP%\notebooklm_dub_automation.py" "!WORKDIR!" --list-lessons
echo.
echo [2/3] Select lesson range
echo   Enter = process ALL lessons
echo   Example: from 1 to 3  -- only lessons 1, 2, 3
echo.
set /p "LFROM=From lesson # (Enter=all): "
if not "!LFROM!"=="" (
    set /p "LTO=To lesson # (Enter=!LFROM!): "
    if "!LTO!"=="" set "LTO=!LFROM!"
    set "LESSON_ARGS=--from-lesson !LFROM! --to-lesson !LTO!"
    echo   Selected: !LFROM! ~ !LTO!
) else (
    echo   Selected: ALL
)
echo.

if "!DODUB!"=="1" (
    set "STAGE=voice_pick"
    echo [2.5/3] Voice gender per video ^(saved to _dub_voice_map.json^)
    echo   f=all female  m=all male  Enter=per-item  s=use saved
    echo.
    %PY% -u "%COMP%\dub_voice_prefs.py" "!WORKDIR!" !LESSON_ARGS!
    if errorlevel 1 (
        set "ERR=1"
        goto END
    )
    echo.
)

:RUN
set "STAGE=translate_dub"
echo.
echo [3/3] Translate (+ dub) ...
echo   Large batches auto-split by file and by cue; may take hours.
echo   You can leave it running; rerun skips completed files.
echo.
if "!DODUB!"=="0" (
    %PY% -u "%COMP%\notebooklm_dub_automation.py" "!WORKDIR!" --no-dub !LESSON_ARGS!
) else (
    %PY% -u "%COMP%\notebooklm_dub_automation.py" "!WORKDIR!" !LESSON_ARGS!
)
set "ERR=!ERRORLEVEL!"

:END
echo.
if "!ERR!" neq "0" (
    if /i "!STAGE!"=="ensure_ready" (
        echo [Hint] Login or deps failed. Retry or run notebooklm_dub_setup.bat manually.
    ) else if /i "!STAGE!"=="voice_pick" (
        echo [Hint] Voice selection interrupted/failed. Re-run and complete selection.
    ) else if /i "!STAGE!"=="translate_dub" (
        echo [Hint] Translation/Dub had failures. Re-run same command for resume ^(_notebooklm_progress.json^).
        echo        Check temp\ and _notebooklm_progress.json in your course folder for failed items.
    ) else (
        echo [Hint] Task failed. Re-run and check console output.
    )
)
pause
endlocal
exit /b %ERR%
