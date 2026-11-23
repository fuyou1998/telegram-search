@echo off
setlocal
cd /d "%~dp0"
if not exist ".env" copy ".env.example" ".env" > nul
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -c "import fastapi, httpx, dotenv, python_socks, telethon, uvicorn" > nul 2>&1
if not errorlevel 1 exit /b 0
".venv\Scripts\python.exe" -m pip --disable-pip-version-check --cache-dir ".venv\pip-cache" install -r requirements.txt
exit /b %ERRORLEVEL%
