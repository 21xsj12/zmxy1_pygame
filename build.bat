@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ============================================================================
rem  Build script for zmxy1_pygame
rem
rem  Packages the game into a standalone Windows application with PyInstaller.
rem  Output: dist\zmxy1_pygame\zmxy1_pygame.exe
rem
rem  Usage:
rem    build.bat              build (pause when finished)
rem    build.bat --no-pause   build without the final pause (for scripts/CI)
rem    build.bat clean        remove build\ and dist\zmxy1_pygame\
rem
rem  Requirements:
rem    Python 3.12+ with pygame-ce (requirements.txt); PyInstaller 6.18+ is
rem    installed automatically when missing.
rem ============================================================================

set "APP_NAME=zmxy1_pygame"
set "ENTRY=main.py"
set "DIST_DIR=dist"
set "BUILD_DIR=build"
set "NO_PAUSE="
set "DO_CLEAN="
set "PYGAME_HIDE_SUPPORT_PROMPT=1"

:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--no-pause" set "NO_PAUSE=1"
if /i "%~1"=="clean" set "DO_CLEAN=1"
shift
goto :parse_args

:args_done
if defined DO_CLEAN goto :clean

echo ============================================================
echo   zmxy1_pygame - PyInstaller build
echo ============================================================
echo.

rem ------------------------------------------------------------ find Python
set "PY_EXE="
set "PY_ARGS="

if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY_EXE=%~dp0.venv\Scripts\python.exe"
  goto :python_found
)

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "PY_EXE=py"
  set "PY_ARGS=-3"
  goto :python_found
)

python -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "PY_EXE=python"
  goto :python_found
)

echo [ERROR] Python not found.
echo         Install Python 3.12+ from https://www.python.org/downloads/
echo         and make sure "Add python.exe to PATH" is enabled.
goto :fail

:python_found
echo [INFO] Python: "%PY_EXE%" %PY_ARGS%
"%PY_EXE%" %PY_ARGS% -V
if errorlevel 1 (
  echo [ERROR] Failed to run Python.
  goto :fail
)

rem --------------------------------------------------------- check packages
"%PY_EXE%" %PY_ARGS% -c "import pygame" >nul 2>nul
if errorlevel 1 (
  echo [INFO] pygame-ce is not installed, installing requirements...
  "%PY_EXE%" %PY_ARGS% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Failed to install pygame-ce.
    echo         Run manually: "%PY_EXE%" %PY_ARGS% -m pip install -r requirements.txt
    goto :fail
  )
)

"%PY_EXE%" %PY_ARGS% -c "import PyInstaller; v=tuple(int(x) for x in PyInstaller.__version__.split('.')[:2]); raise SystemExit(0 if v >= (6, 18) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [INFO] PyInstaller 6.18+ is not installed, installing...
  "%PY_EXE%" %PY_ARGS% -m pip install "pyinstaller>=6.18"
  if errorlevel 1 (
    echo [ERROR] Failed to install PyInstaller.
    echo         Run manually: "%PY_EXE%" %PY_ARGS% -m pip install "pyinstaller>=6.18"
    goto :fail
  )
)

if not exist "%ENTRY%" (
  echo [ERROR] Entry script "%ENTRY%" not found.
  echo         Run this script from the zmxy1_pygame folder.
  goto :fail
)
if not exist "assets\manifest.json" (
  echo [ERROR] assets\manifest.json not found. The assets folder is required.
  goto :fail
)
if not exist "data\actions.json" (
  echo [ERROR] data\actions.json not found. The data folder is required.
  goto :fail
)

rem --------------------------------------------------------------- build
echo.
echo [INFO] Building "%APP_NAME%" (one-folder)...
echo [INFO] This can take several minutes: the game ships 9,400+ asset files.
echo.

rem --contents-directory . keeps sys._MEIPASS == the exe folder, so the game
rem resolves assets/, data/ and saves/ next to the exe (game/constants.py).
rem numpy/yaml are not used by the game; excluding them trims the output.
"%PY_EXE%" %PY_ARGS% -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --log-level WARN ^
  --name "%APP_NAME%" ^
  --contents-directory . ^
  --distpath "%DIST_DIR%" ^
  --workpath "%BUILD_DIR%" ^
  --specpath "%BUILD_DIR%" ^
  --collect-submodules game ^
  --exclude-module numpy ^
  --exclude-module yaml ^
  --add-data "%CD%\assets;assets" ^
  --add-data "%CD%\data;data" ^
  --add-data "%CD%\readme.md;." ^
  "%ENTRY%"
if errorlevel 1 (
  echo.
  echo [ERROR] PyInstaller failed. See the messages above.
  goto :fail
)

rem ------------------------------------------------------------- verify
set "OUT_APP=%DIST_DIR%\%APP_NAME%"
if not exist "%OUT_APP%\%APP_NAME%.exe" (
  echo [ERROR] Build finished but "%OUT_APP%\%APP_NAME%.exe" was not found.
  goto :fail
)
if not exist "%OUT_APP%\assets\manifest.json" (
  echo [ERROR] Bundled assets are missing from "%OUT_APP%\assets".
  goto :fail
)
if not exist "%OUT_APP%\data\actions.json" (
  echo [ERROR] Bundled data is missing from "%OUT_APP%\data".
  goto :fail
)
if not exist "%OUT_APP%\saves" mkdir "%OUT_APP%\saves" >nul 2>nul

echo.
echo ============================================================
echo   [OK] Build succeeded
echo ============================================================
echo   Program : "%CD%\%OUT_APP%\%APP_NAME%.exe"
echo   Folder  : "%CD%\%OUT_APP%"
echo.
echo   Run the exe, or zip the whole "%APP_NAME%" folder to share it.
echo   Keep assets\ and data\ together with the exe; saves go to saves\.
echo.
if not defined NO_PAUSE pause
exit /b 0

rem --------------------------------------------------------------- clean
:clean
echo [INFO] Removing build artifacts...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
if exist "%DIST_DIR%\%APP_NAME%" rmdir /s /q "%DIST_DIR%\%APP_NAME%"
echo [OK] Clean done.
if not defined NO_PAUSE pause
exit /b 0

rem ---------------------------------------------------------------- fail
:fail
echo.
echo [ERROR] Build failed.
if not defined NO_PAUSE pause
exit /b 1
