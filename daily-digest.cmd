@echo off
setlocal
chcp 65001 > nul
cd /d "%~dp0"
set PYTHONUTF8=1
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" daily_digest.py %*
    exit /b %ERRORLEVEL%
)
if exist "python\python.exe" (
    "python\python.exe" daily_digest.py %*
    exit /b %ERRORLEVEL%
)
echo 找不到 Python。请先运行安装脚本或「极目」便携包。
exit /b 1
