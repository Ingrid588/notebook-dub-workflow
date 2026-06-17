@echo off

setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

title Quick Bilingual Dub Preview



set "ROOT=%~dp0"

set "COMP=%ROOT%components"

set "PY=py -3.12"

set "ERR=0"

set "USE_PUSHD=0"



%PY% --version >nul 2>&1

if errorlevel 1 (

    echo [Error] Python 3.12 not found. Run: py -0p

    goto END

)



ffmpeg -version >nul 2>&1

if errorlevel 1 (

    echo [Warn] ffmpeg not found - step2 merge will fail

    echo.

)



echo ==================================================

echo   Quick Bilingual Dub Preview

echo   NO Gemini / NO subtitle optimization

echo   Uses Chinese lines from *bilingual*.srt or *.txt

echo   Speed up only if TTS longer than subtitle window

echo ==================================================

echo.

echo   Route A (learn):  NotebookLM + quick_bilingual_dub.bat
echo   Route B (commercial): fit_translate + commercial_dub_test.bat
echo   See WORKFLOWS.txt
echo.



set "WORKDIR="



if not "%~1"=="" (

    for %%I in ("%~1") do set "WORKDIR=%%~fI"

    echo [Selected] !WORKDIR!

    goto RUN

)



echo Usage:

echo   1. Drag folder onto quick_bilingual_dub_drop.vbs  (best for long paths)

echo   2. Or drag onto this bat

echo   3. Or paste folder path below

echo.



set /p "WORKDIR=Folder path: "

if "!WORKDIR!"=="" goto END

set "WORKDIR=!WORKDIR:"=!"

if "!WORKDIR:~-1!"=="\" set "WORKDIR=!WORKDIR:~0,-1!"

for %%I in ("!WORKDIR!") do set "WORKDIR=%%~fI"



:RUN

if "!WORKDIR!"=="" (

    echo [Error] No folder selected

    goto END

)



pushd "!WORKDIR!" 2>nul

if errorlevel 1 (

    echo [Error] Folder not found or cannot open:

    echo   !WORKDIR!

    goto END

)

set "USE_PUSHD=1"

set "WORKDIR=!CD!"

echo [Working in] !WORKDIR!



echo.

echo [check] packages...

%PY% -u "%COMP%\check_env.py"

if errorlevel 1 goto CLEANUP



set "FIT_ARG="

if /i "%~2"=="local" set "FIT_ARG=--fit local"

if /i "%~2"=="gemini" set "FIT_ARG=--fit gemini"

if defined FIT_ARG echo [Fit mode] %~2

echo.

echo [Voice] Pick male/female per video ^(saved to _dub_voice_map.json^)
echo   f=all female  m=all male  Enter=per-item  s=use saved
echo.
%PY% -u "%COMP%\dub_voice_prefs.py" "!WORKDIR!"
if errorlevel 1 (
    set "ERR=1"
    goto CLEANUP
)
echo.

%PY% -u "%COMP%\quick_bilingual_dub.py" "!WORKDIR!" !FIT_ARG!

set "ERR=!ERRORLEVEL!"



:CLEANUP

if "!USE_PUSHD!"=="1" popd



:END

echo.

if "!ERR!" neq "0" (

    echo [Done with errors] exit code !ERR!

) else (

    echo [Done]

)

echo.

echo Press any key to close...

pause >nul

endlocal


