@echo off
chcp 65001 >nul
title System Cleaner - 打包成 exe
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py"

echo ================================================================
echo  打包 cleaner -> dist\Cleaner.exe / dist\cleaner-cli.exe
echo  首次打包需要先安装 PyInstaller(仅打包需要, 生成的 exe 零依赖)
echo ================================================================

%PY% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo 未检测到 PyInstaller, 正在安装...
    %PY% -m pip install pyinstaller
    if errorlevel 1 (
        echo.
        echo [失败] PyInstaller 安装失败, 请手动执行:
        echo        %PY% -m pip install pyinstaller
        pause >nul
        exit /b 1
    )
)

%PY% build.py
echo.
if errorlevel 1 (
    echo [失败] 打包出错, 请看上面的输出。
) else (
    echo [完成] 产物在 dist 目录:
    echo        dist\Cleaner.exe       图形界面主程序(双击即用, 自动申请管理员)
    echo        dist\cleaner-cli.exe   命令行版
)
echo.
pause >nul
