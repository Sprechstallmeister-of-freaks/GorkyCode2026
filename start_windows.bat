@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

:: Определяем интерпретатор Python
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (set "PY=py") else (set "PY=python")

:: Создаём виртуальное окружение, если нет
if not exist .venv\Scripts\python.exe (
  %PY% -m venv .venv || (echo Failed to create venv & pause & exit /b 1)
)

call .venv\Scripts\activate.bat

:: Устанавливаем зависимости
python -m pip install -r requirements.txt || (echo Dependency installation failed & pause & exit /b 1)

echo.
echo ========================================
echo  API server running on http://127.0.0.1:8000
echo  Swagger: http://127.0.0.1:8000/docs
echo ========================================

:start_server
python run.py

:menu
echo.
echo ========================================
echo  Server stopped
echo  [R] - Restart server
echo  [Q] - Quit
echo ========================================
set /p "choice=Choose action: "
if /i "!choice!"=="R" goto start_server
if /i "!choice!"=="Q" goto end
echo Invalid choice, try again.
goto menu

:end
endlocal
