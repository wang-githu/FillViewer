@echo off
setlocal
cd /d "%~dp0"
set "PY=python"
where python >nul 2>nul || set "PY=py"
echo Starting FillViewer ...
start "" "http://127.0.0.1:8099/"
"%PY%" server.py
pause
