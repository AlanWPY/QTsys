@echo off
cd /d "%~dp0"

REM 检查虚拟环境是否存在
if not exist .dbmanager_venv (
    echo 首次运行，正在创建虚拟环境...
    python -m venv .dbmanager_venv
    echo 安装依赖...
    .dbmanager_venv\Scripts\pip install "PyQt6==6.7.1" "PyQt6-Qt6==6.7.3" pymysql cryptography -i https://mirrors.aliyun.com/pypi/simple/
)

.dbmanager_venv\Scripts\python -m db_manager.main
pause
