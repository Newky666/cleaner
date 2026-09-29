@echo off
chcp 65001 >nul
title System Cleaner - Game Mode (Auto)
setlocal
set "SCRIPT=%~dp0cleaner.py"

rem Pick a Python interpreter (py launcher works even when 'python' is the Store stub)
set "PY=python"
where py >nul 2>nul && set "PY=py"

echo ================================================================
echo  Game Mode: watching for fullscreen games
echo  - boosts the game process (high priority, EcoQoS off)
echo  - trims background memory every 5 minutes
echo  KEEP THIS WINDOW OPEN while playing; close it to restore.
echo ================================================================
%PY% -u "%SCRIPT%" game auto --poll 5 --trim-interval 300 --elevate
echo.
echo Game mode stopped. Press any key to close...
pause >nul
