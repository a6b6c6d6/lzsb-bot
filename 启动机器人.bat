@echo off
chcp 65001 >nul
title linux.sb 自动回帖机器人
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY if exist "D:\python\python.exe" set "PY=D:\python\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "C:\Users\user\.mytools\binaries\python\versions\3.13.12\python.exe" set "PY=C:\Users\user\.mytools\binaries\python\versions\3.13.12\python.exe"

if not defined PY (
  echo [错误] 没有找到 Python，请先安装 Python 3 并在安装时勾选 "Add to PATH"。
  echo.
  pause
  exit /b 1
)

if not exist "config.json" (
  echo [提示] 还没有 config.json，正在从 config.example.json 复制一份……
  copy /y "config.example.json" "config.json" >nul
  echo        请用记事本打开 config.json，填好账号密码和 AI 接口后再运行。
  echo.
  pause
  exit /b 1
)

echo ==================================================
echo   linux.sb 自动回帖机器人
echo   按 Ctrl+C 可随时安全停止
echo ==================================================
echo.

"%PY%" bot.py

echo.
echo 机器人已退出。
pause
