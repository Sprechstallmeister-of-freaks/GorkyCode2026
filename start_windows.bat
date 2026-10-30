@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (set "PY=py") else (set "PY=python")
if not exist .venv\Scripts\python.exe (
  %PY% -m venv .venv || (echo Failed to create venv & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt || (echo Dependency installation failed & pause & exit /b 1)
echo.
echo Open http://127.0.0.1:8000 in your browser
python run.py
pause
