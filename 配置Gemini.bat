@echo off
setlocal EnableExtensions
cd /d "%~dp0components"
title Gemini API Quick Setup

py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python 3.12 not found.
    goto END
)

echo ==================================================
echo   Gemini API quick setup  (Key + model ONLY)
echo   For full options use: 配置.bat
echo   (scope: all cues vs problem cues, Speech Activity)
echo ==================================================
echo.

py -3.12 -u run_batch_course.py --api-keys

:END
echo.
pause >nul
