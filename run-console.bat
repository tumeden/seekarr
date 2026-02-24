@echo off
setlocal

cd /d "%~dp0"

echo ==========================================
echo Seekarr - Console (Worker)
echo ==========================================
echo.
echo This runs Seekarr in the terminal (no web UI).
echo Press Ctrl+C to stop.
echo.
echo Tip: pass --force to do an immediate run at startup:
echo   run-console.bat --force
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python is not installed or not in PATH.
  echo Install Python and try again.
  echo.
  pause
  exit /b 1
)

python -c "import yaml" >nul 2>nul
if errorlevel 1 (
  echo Installing required Python packages...
  python -m pip install -r ".\requirements.txt"
  if errorlevel 1 (
    echo [ERROR] Failed to install requirements.
    echo Try manually: python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
  )
)

if not exist ".\config.yaml" (
  echo [ERROR] config.yaml not found in this folder.
  echo Copy config.example.yaml to config.yaml and configure it first.
  echo.
  pause
  exit /b 1
)

set EXTRA_ARGS=
if /I "%~1"=="--force" set EXTRA_ARGS=--force

python .\main.py --config .\config.yaml %EXTRA_ARGS%

echo.
echo Exit code: %errorlevel%
echo.
pause
exit /b %errorlevel%

