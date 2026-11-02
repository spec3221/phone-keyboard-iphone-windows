@echo off
setlocal
cd /d "%~dp0"
title Phone Keyboard - iPhone to Windows

if not exist ".venv\Scripts\python.exe" (
  echo [1/2] Creating a private Python environment...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo.
    echo Python 3 was not found. Install it from https://www.python.org/downloads/windows/
    echo During installation, enable the option to add Python to PATH.
    pause
    exit /b 1
  )
)

.venv\Scripts\python.exe -c "import aiohttp, qrcode, psutil" >nul 2>&1
if errorlevel 1 (
  echo [2/2] Installing required libraries...
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Could not install libraries. Check your Internet connection and try again.
    pause
    exit /b 1
  )
)

echo.
echo Starting the low-latency keyboard server...
echo Keep this window open while using the phone keyboard.
echo.
.venv\Scripts\python.exe server.py
if errorlevel 1 (
  echo.
  echo The server stopped with an error.
  pause
)
endlocal
