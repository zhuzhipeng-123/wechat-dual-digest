@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment not found. Run: py -3.12 -m venv .venv
  exit /b 1
)
".venv\Scripts\python.exe" -m app.cli
endlocal
