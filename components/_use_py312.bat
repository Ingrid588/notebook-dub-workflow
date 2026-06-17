@echo off
setlocal EnableExtensions
cd /d "%~dp0"

py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python 3.12 not found.
    exit /b 1
)
exit /b 0
