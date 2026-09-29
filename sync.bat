@echo off
chcp 65001 >nul
title System Cleaner - 同步到 GitHub
setlocal
cd /d "%~dp0"

rem 提交说明: 可以当参数传进来, 也可以留空后手工输入
set "MSG=%~1"
if "%MSG%"=="" (
    set /p MSG=请输入本次提交说明:
)
if "%MSG%"=="" (
    echo [取消] 提交说明不能为空。
    pause >nul
    exit /b 1
)

echo ================================================================
echo  同步 %~dp0  ->  https://github.com/Newky666/cleaner
echo ================================================================

rem 先把远端的改动拉回来(rebase 让历史保持一条直线)
git fetch origin
git pull --rebase origin main
if errorlevel 1 (
    echo [中止] 拉取远端失败或有冲突, 请手动处理后再同步。
    pause >nul
    exit /b 1
)

rem 暂存所有改动(.gitignore 会自动过滤日志/配置等运行期文件)
git add -A
git diff --cached --quiet && (
    echo 没有需要提交的改动, 直接推送...
) || (
    git -c user.name=Newky -c user.email=newkyneteasy@163.com commit -m "%MSG%"
)

git push origin main
if errorlevel 1 (
    echo.
    echo [失败] 推送失败。若提示需要登录, 请先执行一次:
    echo        git credential-manager github login
) else (
    echo.
    echo [完成] 已同步到 https://github.com/Newky666/cleaner
)

echo.
pause >nul
