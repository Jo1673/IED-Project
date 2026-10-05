@echo off
rem Starts the vibration monitor. Add --dry-run to log without touching the printer.
cd /d %~dp0
call .venv\Scripts\activate.bat
python monitor\monitor.py run
pause
