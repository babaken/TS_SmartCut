@echo off
rem TS_SmartCut launcher for Windows (counterpart of scripts\run_ts_smartcut.sh).
rem Starts ts_smartcut.main with the project .venv, without a console window.
rem Run with /console to keep the console open and see error messages.
rem Keep this file ASCII-only: cmd.exe reads .bat files in the ANSI code page.
setlocal
rem "python -m" needs the project root as the current directory.
cd /d "%~dp0.."
if not exist ".venv\Scripts\pythonw.exe" (
    echo .venv not found in %CD%
    echo See docs\TS_SmartCut_Windows_Setup.md to create it.
    pause
    exit /b 1
)
if /i "%~1"=="/console" (
    ".venv\Scripts\python.exe" -m ts_smartcut.main
    pause
    exit /b
)
start "" ".venv\Scripts\pythonw.exe" -m ts_smartcut.main
