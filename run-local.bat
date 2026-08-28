@echo off
REM One-command local launcher (Windows).
REM   run-local.bat        -> trading loop + dashboard at http://localhost:8080
REM   run-local.bat once   -> run a single cycle and exit
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python not found. Install it from python.org - tick "Add Python to PATH" - then re-run.
  pause & exit /b 1
)

if not exist .venv (
  echo [*] Creating a private Python environment ^(one time^)...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt

if not exist .env (
  copy .env.example .env >nul
  echo.
  echo [KEY] Created a .env file for your API keys.
  echo       Open it in Notepad and fill in ANTHROPIC_API_KEY, ALPACA_API_KEY,
  echo       ALPACA_SECRET_KEY, then run this again.
  echo       ^(.env is git-ignored - your keys never leave this computer.^)
  pause & exit /b 1
)

if "%1"=="once" (
  echo [*] Running a single cycle...
  python -m bot.run
  pause & exit /b 0
)

echo.
echo [OK] Starting. Dashboard: http://localhost:8080   ^(Ctrl+C to stop^)
echo.
python -m bot.serve
pause
