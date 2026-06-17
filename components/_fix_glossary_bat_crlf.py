#!/usr/bin/env python3
"""Rewrite glossary_dub.bat with Windows CRLF (cmd.exe requires it)."""
from pathlib import Path

BAT = Path(__file__).resolve().parent.parent / "glossary_dub.bat"

CONTENT = r"""@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Glossary Fix + Dub (zero API)

set "ROOT=%~dp0"
set "COMP=%ROOT%components"
set "ERR=0"
set "USE_PUSHD=0"
set "PY="

py -3.12 --version >nul 2>&1
if not errorlevel 1 set "PY=py -3.12"
if "!PY!"=="" (
    py -3 --version >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if "!PY!"=="" (
    python --version >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if "!PY!"=="" (
    echo [Error] Python not found. Install 3.12+ or add py/python to PATH.
    set "ERR=1"
    goto END
)

ffmpeg -version >nul 2>&1
if errorlevel 1 echo [Warn] ffmpeg not found - merge step may fail

set "WORKDIR="
if not "%~1"=="" (
    for %%I in ("%~1") do set "WORKDIR=%%~fI"
    goto RUN
)

echo Drag folder here or paste path:
echo   Needs: video + *merge*.srt or *bilingual*.srt
set /p "WORKDIR=Folder: "
if "!WORKDIR!"=="" goto END
set "WORKDIR=!WORKDIR:"=!"
for %%I in ("!WORKDIR!") do set "WORKDIR=%%~fI"

:RUN
if not exist "!WORKDIR!\" (
    echo [Error] Not found: !WORKDIR!
    set "ERR=1"
    goto END
)

echo ==================================================
echo   Glossary fix + Dub  (zero translation API)
echo   1) Term fix on merge/bilingual SRT
echo   2) Local fit compress
echo   3) Pick male/female voice per video
echo   4) TTS + merge _C.mp4
echo   Folder: !WORKDIR!
echo ==================================================
echo.

pushd "!WORKDIR!" 2>nul
if errorlevel 1 (
    echo [Error] Cannot open folder
    set "ERR=1"
    goto END
)
set "USE_PUSHD=1"
set "WORKDIR=!CD!"

echo [0/4] Check packages...
!PY! -u "!COMP!\check_env.py"
if errorlevel 1 (
    set "ERR=1"
    goto CLEANUP
)

echo.
echo [1/4] Glossary fix -^> *.term-fixed.srt
!PY! -u "!COMP!\apply_glossary_to_subs.py" "!WORKDIR!"
if errorlevel 1 (
    set "ERR=1"
    goto CLEANUP
)

echo.
echo [2/4] Voice gender per video
echo   f=all female  m=all male  Enter=per-item  s=use saved
!PY! -u "!COMP!\dub_voice_prefs.py" "!WORKDIR!"
if errorlevel 1 (
    set "ERR=1"
    goto CLEANUP
)

echo.
echo [3/4] Dub: merge + fit compress + TTS (--fit local)
!PY! -u "!COMP!\quick_bilingual_dub.py" "!WORKDIR!" --fit local
if errorlevel 1 (
    set "ERR=1"
    goto CLEANUP
)

echo.
echo Done. Output: *_C.mp4
echo   Subtitles: *.term-fixed.srt -^> *.quick-fit.srt
echo   Pacing: *.pacing.json

:CLEANUP
if "!USE_PUSHD!"=="1" popd

:END
echo.
if "!ERR!" neq "0" (
    echo [Failed] exit code !ERR!
    echo   Tip: re-run to resume Edge TTS from cache if interrupted.
) else (
    echo [OK] Glossary + dub finished
)
pause
endlocal & exit /b !ERR!
"""

def main() -> None:
    data = CONTENT.replace("\n", "\r\n").encode("utf-8")
    BAT.write_bytes(data)
    raw = BAT.read_bytes()
    crlf = raw.count(b"\r\n")
    lf_only = raw.count(b"\n") - crlf
    print(f"Wrote {BAT}")
    print(f"CRLF lines: {crlf}, bare LF: {lf_only}")


if __name__ == "__main__":
    main()
