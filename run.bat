@chcp 65001 >nul
@echo off
REM Windows 上双击运行。请先：1) 登录微信桌面版 2) copy config.example.yaml config.yaml 并填好
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist .venv (
    echo [1/2] 创建虚拟环境...
    python -m venv .venv
    call .venv\Scripts\activate.bat
    python -m pip install -U pip
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

echo [2/2] 启动监控客户端（Ctrl+C 退出）...
python -m wxclient.main
pause
