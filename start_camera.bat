@echo off
rem Starts the webcam server. Change --index if the Brio is not camera 0.
cd /d %~dp0
call .venv\Scripts\activate.bat
python camera\camera_server.py --index 0 --width 1280 --height 720 --fps 15
pause
