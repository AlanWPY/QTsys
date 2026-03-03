@echo off
cd /d "%~dp0"
.dbmanager_venv\Scripts\python -m db_manager.main
pause
