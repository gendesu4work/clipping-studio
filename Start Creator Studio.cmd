@echo off
cd /d "%~dp0"
echo Starting Creator Studio on http://127.0.0.1:7860 ...
if exist ".venv\Scripts\python.exe" (".venv\Scripts\python.exe" server.py) else (python server.py)
pause
