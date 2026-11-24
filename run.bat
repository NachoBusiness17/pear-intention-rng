@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if %ERRORLEVEL%==0 (
  uv run app.py
  exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if not %ERRORLEVEL%==0 (
  echo Error: Python 3.11+ not found. Install Python or open public\index.html in your browser.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating local virtual environment (.venv^) and installing dependencies...
  python -m venv .venv
  if errorlevel 1 exit /b 1
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\pip.exe" install -r requirements.txt
  if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" app.py
