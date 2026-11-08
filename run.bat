@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" main.py
  if errorlevel 1 pause
  exit /b
)

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.12+ and run:
  echo         python -m pip install -r requirements.txt
  pause
  exit /b 1
)

python main.py
if errorlevel 1 pause
