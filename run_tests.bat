@echo off
chcp 65001 >nul
title System Cleaner - Tests
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py"

echo ================================================================
echo  运行回归测试 (python -m unittest discover -s tests)
echo ================================================================
%PY% -m unittest discover -s tests -t . -v
echo.
if errorlevel 1 (
    echo [失败] 有用例未通过。
) else (
    echo [通过] 全部用例通过。
)
pause >nul
