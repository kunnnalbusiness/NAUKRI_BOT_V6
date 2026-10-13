@echo off
title Stop Naukri Bot
color 0C

cd /d "%~dp0"

echo ===================================================
echo        STOPPING NAUKRI BOT SERVERS
echo ===================================================
echo.

echo Terminating Backend and Frontend processes...
taskkill /fi "WINDOWTITLE eq Naukri Bot Backend*" /f /t >nul 2>&1
taskkill /fi "WINDOWTITLE eq Naukri Bot Frontend*" /f /t >nul 2>&1

echo.
echo [✓] All servers stopped successfully.
echo.
pause
