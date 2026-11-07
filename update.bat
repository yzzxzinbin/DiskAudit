@echo off
title DiskAudit - 刷新数据
cd /d "%~dp0"

rem 只刷新，不起服务、不开浏览器：扫描 → 补齐文字层 → 生成 ticket，然后退出。
rem 想跳过扫描（只刷新文字层）就带 --skip-scan。
rem 参数会原样转给 scripts\app.py，例如：update.bat --skip-scan
rem 双击运行时会先列出本机磁盘让你选；脚本里调用加 --no-ask 跳过提问

set "PY="
where py.exe >nul 2>nul
if %errorlevel% equ 0 (
  set "PY=py.exe"
  goto :run
)
where python.exe >nul 2>nul
if %errorlevel% equ 0 (
  set "PY=python.exe"
  goto :run
)

echo.
echo   [x] 没找到 Python。请安装 Python 3 并勾选 "Add to PATH"。
echo.
pause
exit /b 1

:run
%PY% "scripts\app.py" --update %*
set "RC=%errorlevel%"

rem 双击运行时（没有参数）停下来让人看结果；带参数当命令用就直接退
if "%~1"=="" pause
exit /b %RC%
