@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title NotebookLM Setup (one-time login)

set "PY=py -3.12"
set "COMP=%~dp0components"

echo ==================================================
echo   NotebookLM semi-auto setup (experimental)
echo   Unofficial API - personal / prototype use only
echo ==================================================
echo.

%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python 3.12 not found.
    goto END
)

echo [1/4] Installing notebooklm-py ...
%PY% -m pip install "notebooklm-py[browser]" rookiepy -q
if errorlevel 1 goto END

echo.
echo [2/4] Installing Playwright Chromium (first run only, ~180MB) ...
%PY% -m playwright install chromium
if errorlevel 1 (
    echo [Error] playwright install failed. Retry or run manually:
    echo   %PY% -m playwright install chromium
    goto END
)

echo.
echo [3/4] Login to Google NotebookLM (browser opens once)
echo       Sign in with the account that can use NotebookLM.
echo.
%PY% -m notebooklm login
if errorlevel 1 (
    echo [Error] Login failed.
    goto END
)

echo.
echo [4/4] Verify auth ...
%PY% -m notebooklm auth check --test --json
echo.
echo Done. Same as notebooklm_dub_translate.bat (login is built-in there).

:END
echo.
pause
endlocal
