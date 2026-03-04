@echo off
cd /d "%~dp0"

REM 检查是否安装了 PyQt6
python -c "import PyQt6" 2>nul
if errorlevel 1 (
    echo 检测到缺少 PyQt6，正在安装...
    pip install PyQt6==6.7.1 PyQt6-Qt6==6.7.3 pymysql cryptography
    if errorlevel 1 (
        echo 安装失败，请手动运行: pip install PyQt6==6.7.1 PyQt6-Qt6==6.7.3 pymysql cryptography
        pause
        exit /b 1
    )
)

python -m db_manager.main
pause
