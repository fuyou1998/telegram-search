@echo off
setlocal
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
call "%~dp0setup.cmd"
if errorlevel 1 goto finished
".venv\Scripts\python.exe" telegram_login.py
:finished
set "LOGIN_RESULT=%ERRORLEVEL%"
pause
exit /b %LOGIN_RESULT%
