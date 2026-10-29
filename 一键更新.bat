@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

title 数据采集流水线

echo.
echo ==========================================================
echo    数据采集流水线
echo    采集 - 去重入库 - TCP 预筛 - 模拟网页访问 - 导出
echo ==========================================================
echo.

REM ---------- 环境检查 ----------
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 没找到 .venv 虚拟环境。
    echo        请在项目目录执行一次：
    echo          python -m venv .venv
    echo          .venv\Scripts\pip install requests pyyaml PySocks
    echo.
    goto :fail
)

".venv\Scripts\python.exe" -c "import requests,yaml,socks" >nul 2>&1
if errorlevel 1 (
    echo [提示] 缺少依赖，正在安装...
    ".venv\Scripts\python.exe" -m pip install -q requests pyyaml PySocks
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络。
        goto :fail
    )
)

echo 开始运行，中途别关窗口。首次跑会下载测速内核，之后就不用等了。
echo.

REM ---------- 主流程 ----------
".venv\Scripts\python.exe" run.py all
if errorlevel 1 (
    echo.
    echo [错误] 运行出错了，上面有详细信息。
    goto :fail
)

REM ---------- 打开结果目录 ----------
if exist "output" start "" "output"

echo.
echo 按任意键关闭...
pause >nul
exit /b 0

:fail
echo.
echo 运行失败。可以单独跑某一步排查：
echo    .venv\Scripts\python.exe run.py collect
echo    .venv\Scripts\python.exe run.py check
echo.
echo 按任意键关闭...
pause >nul
exit /b 1
