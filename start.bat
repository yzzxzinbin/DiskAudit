@echo off
title DiskAudit
cd /d "%~dp0"

rem 首次运行会自动初始化 + 扫描 + 生成 ticket；之后启动只是导入 ticket 再起服务。
rem 所有逻辑都在 scripts\app.py，这里只负责找到 Python 并转发参数。

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
%PY% "scripts\app.py" %*
if %errorlevel% neq 0 (
  echo.
  pause
)
