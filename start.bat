@echo off
title Naukri & LinkedIn Bot Launcher
color 0A

:: Navigate to project root directory
cd /d "%~dp0"

echo ===================================================
echo        NAUKRI ^& LINKEDIN AUTOMATION BOT
echo ===================================================
echo.

:: 1. Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH!
    echo Please install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

:: 2. Check Node / npm
npm --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Node.js / npm is not installed or not in PATH!
    echo Please install Node.js and add it to PATH.
    pause
    exit /b 1
)

echo [OK] Python and Node.js verified.
echo.

:: 3. Start Backend server
echo [1/3] Starting FastAPI Backend on port 8000...
start "Naukri Bot Backend" cmd /k "title Naukri Bot Backend && color 0B && python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000"

:: 4. Start Frontend server
echo [2/3] Starting React Frontend on port 5173...
start "Naukri Bot Frontend" cmd /k "title Naukri Bot Frontend && color 0E && cd /d "%~dp0frontend" && npm run dev"

:: 5. Wait for servers to spin up
echo [3/3] Waiting for servers to initialize...
timeout /t 4 /nobreak >nul

:: 6. Launch browser
echo Opening Dashboard in browser: http://localhost:5173
start http://localhost:5173

echo.
echo ===================================================
echo   Automation servers are now running!
echo   - Backend:  http://127.0.0.1:8000
echo   - Frontend: http://localhost:5173
echo.
echo   Keep the server terminal windows open while using.
echo   To stop, simply close the windows or run stop.bat.
echo ===================================================
echo.
pause
