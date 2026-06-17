@echo off
cd /d "%~dp0"
py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [Error] Python 3.12 not found.
    goto END
)
py -3.12 -u "%~dp0preview_local_voices.py" --gender male
:END
pause
