@echo off
python "%~dp0installer\setup_wizard.py"
if %ERRORLEVEL% NEQ 0 (
    echo Python not found. Please install Python 3.10+ from https://python.org/downloads
    pause
)

