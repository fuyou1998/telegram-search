@echo off
setlocal
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
call "%~dp0setup.cmd"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" launcher.py
if not errorlevel 1 exit /b 0
:failed
pause
exit /b 1
