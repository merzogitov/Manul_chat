@echo off
chcp 65001 >nul
set APP_ENV=production
python app.py
pause
