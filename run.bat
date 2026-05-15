@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto :have_venv

echo [docregistrar] Creating virtual environment with Python 3.14...
py -3.14 -m venv .venv
if not errorlevel 1 goto :have_venv

echo [docregistrar] py -3.14 not found, falling back to default Python launcher...
py -m venv .venv
if not errorlevel 1 goto :have_venv

echo [docregistrar] ERROR: Could not create venv. Make sure Python 3.12 or newer is installed.
pause
exit /b 1

:have_venv

call ".venv\Scripts\activate.bat"

if exist ".venv\.installed" goto :have_deps

echo [docregistrar] Installing dependencies (one-time, can take a few minutes)...
python -m pip install --upgrade pip
if errorlevel 1 goto :pip_failed

python -m pip install -r requirements.txt
if errorlevel 1 goto :pip_failed

> ".venv\.installed" echo done
goto :have_deps

:pip_failed
echo [docregistrar] ERROR: pip install failed.
pause
exit /b 1

:have_deps

echo.
echo [docregistrar] Starting server at http://127.0.0.1:8000/
echo [docregistrar] Make sure LM Studio is running with the model loaded
echo [docregistrar] and its server enabled at http://localhost:1234
echo.

python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

endlocal