@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment was not found.
  echo Please complete the installation steps in README.md first.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" ui_server.py
if errorlevel 1 pause
