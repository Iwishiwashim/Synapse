@echo off
python "%~dp0installer\setup_wizard.py"
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Python not found. Install Python 3.10+ from https://python.org/downloads
    pause
)
