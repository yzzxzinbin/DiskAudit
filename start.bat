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
set "RC=%errorlevel%"

rem 端口被占时 app.py 会自动顺延（显式 --port 除外）；只有真起不来才停在这里。
rem 这里必须把退出码传出去，否则 pause 会把 errorlevel 洗成 0，脚本里调用看不出失败。
if %RC% neq 0 (
  echo.
  echo   启动失败，退出码 %RC%。具体原因见上方。
  rem 和 update.bat 一个口径：带参数是脚本调用，直接退，别卡在 pause 等人按键。
  if "%~1"=="" (
    echo   按任意键关闭。
    pause >nul
  )
)
exit /b %RC%
