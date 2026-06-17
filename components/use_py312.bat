@echo off
set "PY312="
where py >nul 2>&1
if not errorlevel 1 (
    py -3.12 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY312=1"
)
if not defined PY312 (
    echo [Error] Python 3.12 not found.
    echo         Install Python 3.12, then run: py -0p
    echo         Do NOT use default python 3.13 for Kokoro.
    exit /b 1
)
exit /b 0
