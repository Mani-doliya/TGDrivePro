@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    py -3 -m venv .venv
    if errorlevel 1 (
        echo Python 3 installation was not found. Install Python 3.10 or newer.
        pause
        exit /b 1
    )
)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe TGDrive.py
if errorlevel 1 pause
