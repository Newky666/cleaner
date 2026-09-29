@echo off
chcp 65001 >nul
title System Cleaner - GUI
setlocal
set "SCRIPT=%~dp0cleaner.py"

rem Pick a Python interpreter (prefer the windowed one so no console flashes)
set "PY="
where pyw >nul 2>nul && set "PY=pyw"
if not defined PY if exist "C:\Program Files\Python310\pythonw.exe" set "PY=C:\Program Files\Python310\pythonw.exe"
if not defined PY if exist "C:\Program Files\Python310\python.exe" set "PY=C:\Program Files\Python310\python.exe"
if not defined PY set "PY=python"

rem --elevate: cleanup tool itself pops UAC when not running as administrator
start "" "%PY%" "%SCRIPT%" gui --elevate
