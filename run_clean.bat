@echo off
chcp 65001 >nul
title System Cleaner - Deep Clean
setlocal
set "SCRIPT=%~dp0cleaner.py"

rem Pick a Python interpreter (py launcher works even when 'python' is the Store stub)
set "PY=python"
where py >nul 2>nul && set "PY=py"

echo ================================================================
echo  System Cleaner - Deep Clean
echo ================================================================
%PY% -u "%SCRIPT%" mem clean -p deep -y --elevate
echo.
echo Press any key to close...
pause >nul
