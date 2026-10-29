@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM 用 pythonw 启动，不留黑窗口
if not exist ".venv\Scripts\pythonw.exe" (
    echo [错误] 没找到 .venv 虚拟环境。
    echo        请在项目目录执行：
    echo          python -m venv .venv
    echo          .venv\Scripts\pip install requests pyyaml PySocks
    echo.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "gui.py"
exit /b 0
