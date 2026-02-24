@echo off
setlocal

cd /d "%~dp0"

echo ==========================================
echo Seekarr - Web UI
echo ==========================================
echo Opening local UI at http://127.0.0.1:8788
echo Press Ctrl+C to stop.
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python is not installed or not in PATH.
  pause
  exit /b 1
)

python -c "import flask, yaml, waitress" >nul 2>nul
if errorlevel 1 (
  echo Installing required Python packages...
  python -m pip install -r ".\requirements.txt"
  if errorlevel 1 (
    echo [ERROR] Failed to install requirements.
    pause
    exit /b 1
  )
)

if not exist ".\config.yaml" (
  echo [ERROR] config.yaml not found.
  pause
  exit /b 1
)

python .\webui_main.py --config .\config.yaml

echo.
pause
exit /b %errorlevel%
