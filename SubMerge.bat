@echo off
rem Double-click to start SubMerge.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during installation.
    pause
    exit /b 1
)

rem First run only: install the libraries used for romaji and word colouring.
python -c "import pykakasi, fugashi, unidic_lite" >nul 2>nul
if errorlevel 1 (
    echo Installing the Japanese libraries - first run only, please wait...
    python -m pip install pykakasi fugashi unidic-lite
    if errorlevel 1 (
        echo Installation failed. SubMerge still works, but without romaji and word colouring.
        pause
    )
)

rem pythonw runs the window without keeping this console open.
start "" pythonw submerge.py
